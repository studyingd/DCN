"""
Operating system detection via SSH banner grabbing and TCP port probing.

Detects OS by connecting to common ports (SSH:22, RDP:3389, SMB:445)
and parsing the SSH protocol banner string that every SSH server sends
on connect — no authentication required.

SSH banner format:  SSH-2.0-<software> [comments]
    SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.4  →  Ubuntu Linux
    SSH-2.0-OpenSSH_for_Windows_8.1          →  Windows
    SSH-2.0-Cisco-1.25                       →  Cisco IOS
    SSH-2.0-dropbear_2022.82                 →  Linux (embedded)
"""

import asyncio
import logging
import re
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default timeout per probe (seconds) — keep fast for UX
DEFAULT_TIMEOUT = 3.0

# ── SSH banner patterns → (os_family, os_version_hint) ────────────────
# Order matters: more-specific patterns first.
_BANNER_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ── Known OS distributions ──
    (re.compile(r"Ubuntu[-\s](\S+)", re.I), "linux", "Ubuntu"),
    (re.compile(r"Debian[-\s](\S+)", re.I), "linux", "Debian"),
    (re.compile(r"CentOS[-\s](\S+)", re.I), "linux", "CentOS"),
    (re.compile(r"Rocky[-\s](\S+)", re.I), "linux", "Rocky Linux"),
    (re.compile(r"AlmaLinux[-\s](\S+)", re.I), "linux", "AlmaLinux"),
    (re.compile(r"Fedora[-\s](\S+)", re.I), "linux", "Fedora"),
    (re.compile(r"RHEL[-\s](\S+)", re.I), "linux", "RHEL"),
    (re.compile(r"Red\s*Hat[-\s](\S+)", re.I), "linux", "RHEL"),
    (re.compile(r"SUSE[-\s](\S+)", re.I), "linux", "SUSE"),
    (re.compile(r"openSUSE[-\s](\S+)", re.I), "linux", "openSUSE"),
    (re.compile(r"Arch[-\s]Linux", re.I), "linux", "Arch Linux"),
    (re.compile(r"Raspbian[-\s](\S+)", re.I), "linux", "Raspbian"),
    (re.compile(r"Kali[-\s](\S+)", re.I), "linux", "Kali"),
    (re.compile(r"FreeBSD[-\s](\S+)", re.I), "linux", "FreeBSD"),  # Unix-like
    # ── Vendors / appliances ──
    (re.compile(r"OpenSSH_for_Windows[_\s](\S+)", re.I), "windows", "Windows"),
    (re.compile(r"Cisco[-\s](\S+)", re.I), "switch", "Cisco IOS"),
    (re.compile(r"Juniper[-\s](\S+)", re.I), "switch", "Juniper"),
    (re.compile(r"Huawei[-\s](\S+)", re.I), "switch", "Huawei"),
    (re.compile(r"Dropbear[_\s](\S+)", re.I), "linux", "Linux (Dropbear)"),
    (re.compile(r"ROS[_\s](\S+)", re.I), "router", "MikroTik RouterOS"),
    (re.compile(r"VMware[-\s](\S+)", re.I), "server", "VMware ESXi"),
    (re.compile(r"ESXi[-\s](\S+)", re.I), "server", "VMware ESXi"),
    # ── Generic OpenSSH (no OS comment → assume Linux) ──
    (re.compile(r"OpenSSH[_\s](\S+)", re.I), "linux", "Linux (OpenSSH)"),
]


@dataclass
class OSDetectResult:
    """Result of OS detection probe."""

    ip_address: str
    os_system: str = ""  # "linux" / "windows" / "switch" / "router" / "unknown"
    os_version: str = ""  # human-readable version hint e.g. "Ubuntu 22.04"
    ssh_banner: str | None = None  # raw SSH banner string
    ssh_port_open: bool = False
    rdp_port_open: bool = False
    smb_port_open: bool = False
    confidence: str = "low"  # "high" / "medium" / "low"
    detail: str = ""  # human-readable summary

    def to_dict(self) -> dict:
        return {
            "ip_address": self.ip_address,
            "os_system": self.os_system,
            "os_version": self.os_version,
            "ssh_banner": self.ssh_banner,
            "ssh_port_open": self.ssh_port_open,
            "rdp_port_open": self.rdp_port_open,
            "smb_port_open": self.smb_port_open,
            "confidence": self.confidence,
            "detail": self.detail,
        }


async def _tcp_connect(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Return True if the TCP port accepts a connection."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError, ValueError):
        return False


# ── SMB dialect → Windows version mapping ──────────────────────────
# The SMBv2 negotiate response includes a DialectRevision field.
_SMB_DIALECT_MAP: dict[int, str] = {
    0x0311: "Windows 10 / Server 2016+",
    0x0302: "Windows 8.1 / Server 2012 R2",
    0x0300: "Windows 8 / Server 2012",
    0x0210: "Windows 7 / Server 2008 R2",
    0x0202: "Windows Vista / Server 2008",
}


async def _probe_smb_version(host: str, timeout: float = 3.0) -> str:
    """Send a SMB1 COM_NEGOTIATE to port 445 and extract Windows version.

    The server responds with an SMB2 NEGOTIATE if it supports SMB2+,
    containing a DialectRevision we map to a Windows version range.

    This is an unauthenticated probe — no credentials needed.
    Returns a human-readable version string like "Windows 10 / Server 2016+",
    or empty string if the probe fails.
    """
    # ── SMB1 COM_NEGOTIATE request (Direct TCP transport) ────────────
    # Byte-identical to what pysmb sends; Windows servers respond to this.
    _SMB1_BODY = bytes.fromhex(
        "ff534d42"  # SMB1 protocol
        "72"  # Command: COM_NEGOTIATE
        "00000000"  # Status
        "18"  # Flags: case-insensitive + canonicalized
        "41c8"  # Flags2
        "0000"  # PID_High
        "0000000000000000"  # SecurityFeatures
        "0000"  # Reserved
        "0000"  # TID
        "b0b2"  # PID
        "0000"  # UID
        "0100"  # MID = 1
        "00"  # WordCount = 0
        "1700"  # ByteCount = 23 (little-endian)
        "024e54204c4d20302e313200"  # \x02 + "NT LM 0.12" + \x00
        "02534d4220322e30303200"  # \x02 + "SMB 2.002" + \x00
    )
    _SMB1_PACKET = struct.pack(">I", len(_SMB1_BODY)) + _SMB1_BODY

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 445), timeout=timeout
        )
        try:
            writer.write(_SMB1_PACKET)
            await writer.drain()

            raw_len = await asyncio.wait_for(reader.read(4), timeout=timeout)
            if len(raw_len) < 4:
                return ""
            body_len = struct.unpack(">I", raw_len)[0]
            if body_len < 64 or body_len > 65535:
                return ""

            body = await asyncio.wait_for(reader.read(body_len), timeout=timeout)
            full = raw_len + body

            # Parse response: offset 4 = start of SMB data
            smb_start = 4
            proto = full[smb_start]

            if proto == 0xFE:
                # ── SMB2 NEGOTIATE response ──
                if len(full) < smb_start + 74:
                    return ""
                # SMB2 header: 64 bytes. Negotiate body at smb_start + 64.
                body_start = smb_start + 64
                resp_struct = struct.unpack_from("<H", full, body_start)[0]
                if resp_struct != 65:  # SMB2 NEGOTIATE response struct
                    return ""
                dialect = struct.unpack_from("<H", full, body_start + 4)[0]
                # The negotiated dialect is the LOWEST we offered (0x0202),
                # because the server jumps directly to SMB2 from our SMB1
                # negotiate.  The server almost certainly supports higher
                # dialects; we simply cannot determine the maximum without
                # a full SMB2 multi-dialect negotiate (which Windows SMB
                # driver intercepts on the second connection).
                #
                # Map to the minimum Windows version this dialect implies.
                known = _SMB_DIALECT_MAP.get(dialect)
                if known:
                    return f"Windows ({known}+)"
                return f"Windows (SMB 0x{dialect:04X}+)"

            elif proto == 0xFF:
                # ── SMB1 NEGOTIATE response ──
                wc = full[smb_start + 32]
                if wc >= 1:
                    dialect_idx = struct.unpack_from("<H", full, smb_start + 33)[0]
                    if dialect_idx == 1:  # SMB2 chosen
                        return "Windows (SMB 2.x+)"
                    elif dialect_idx == 0:  # SMB1 only
                        return "Windows (SMB 1.x)"

                # Try to extract Windows string from response data
                data_offset = smb_start + 33 + wc * 2 + 2
                try:
                    ascii_text = full[data_offset:].decode("ascii", errors="ignore")
                    if "Windows" in ascii_text:
                        return "Windows"
                except Exception:
                    pass
                return "Windows (SMB open)"

            return ""

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    except (asyncio.TimeoutError, OSError, ValueError):
        pass

    return ""


async def _grab_ssh_banner(
    host: str, port: int = 22, timeout: float = DEFAULT_TIMEOUT
) -> str | None:
    """Connect to an SSH port and return the server banner string.

    SSH servers MUST send their banner before the client. This is defined
    in RFC 4253 §4.2: the server sends "SSH-<version>-<software> [comments]\\r\\n"
    immediately after the TCP handshake. We read just that one line.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        try:
            banner = await asyncio.wait_for(reader.readline(), timeout=timeout)
            banner_str = banner.decode("utf-8", errors="replace").strip()
            writer.close()
            await writer.wait_closed()
            return banner_str if banner_str.startswith("SSH-") else None
        except (asyncio.TimeoutError, UnicodeDecodeError):
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return None
    except (asyncio.TimeoutError, OSError, ValueError):
        return None


def _parse_banner(banner: str) -> tuple[str, str]:
    """Parse an SSH banner string → (os_system, os_version).

    Returns ("unknown", "") if the banner cannot be identified.
    """
    for pattern, os_family, version_hint in _BANNER_PATTERNS:
        m = pattern.search(banner)
        if m:
            version_str = m.group(1) if m.lastindex and m.lastindex >= 1 else ""
            if version_str:
                ver_detail = f"{version_hint} {version_str}"
            else:
                ver_detail = version_hint
            return os_family, ver_detail

    # Banner starts with SSH- but we don't recognize the software
    if banner.startswith("SSH-"):
        sw_match = re.search(r"SSH-\d+\.\d+-(\S+)", banner)
        if sw_match:
            return "unknown", sw_match.group(1)
        return "unknown", "Unknown SSH server"

    return "unknown", ""


async def _probe_os_via_ssh(
    ip_address: str,
    username: str,
    password: str,
    port: int = 22,
    timeout: float = 8.0,
    pinned_key: str | None = None,
) -> str | None:
    """SSH into a host and run ``cat /etc/os-release`` to get the exact distro.

    Returns the pretty OS name (e.g. "Rocky Linux 10.0 (Red Quartz)")
    or ``None`` if the probe fails.
    """
    from app.services.ssh import HostKeyMismatchError, exec_ssh_command, open_ssh_client

    def _do_ssh() -> str | None:
        try:
            client, _key = open_ssh_client(
                ip_address,
                port,
                username,
                password,
                timeout=int(timeout),
                pinned_key_b64=pinned_key,
            )
        except (HostKeyMismatchError, OSError, Exception):
            return None
        try:
            _exit, output, _err = exec_ssh_command(
                client,
                "cat /etc/os-release 2>/dev/null; echo '---'; ver 2>/dev/null",
                timeout=int(timeout),
            )

            # Parse /etc/os-release (Linux) or ver (Windows)
            lines = output.splitlines()
            pretty_name = ""
            version_id = ""
            for line in lines:
                if line.startswith("PRETTY_NAME="):
                    pretty_name = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    version_id = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("---"):
                    # separator reached — if we already have pretty_name, stop
                    if pretty_name:
                        break

            if pretty_name:
                return pretty_name

            # Fallback: combine NAME + VERSION
            name = ""
            for line in lines:
                if line.startswith("NAME=") and not line.startswith("PRETTY_NAME"):
                    name = line.split("=", 1)[1].strip().strip('"')
                    break
            if name:
                if version_id:
                    return f"{name} {version_id}"
                return name

            # Windows fallback: parse `ver` output
            for line in lines:
                stripped = line.strip()
                if stripped and "Microsoft Windows" in stripped:
                    return stripped
                if stripped and stripped.startswith("Windows"):
                    return stripped

            return None
        except Exception:
            return None
        finally:
            client.close()

    return await asyncio.to_thread(_do_ssh)


async def detect_os(
    ip_address: str,
    timeout: float = DEFAULT_TIMEOUT,
    username: str | None = None,
    password: str | None = None,
    ssh_port: int = 22,
) -> OSDetectResult:
    """Probe an IP address to detect its operating system.

    Strategy:
      1. Grab SSH banner from port 22 → parse for OS hints (highest confidence).
      2. Probe RDP (3389) and SMB (445) as Windows indicators in parallel.
      3. Combine all signals into a confidence score.
    """
    result = OSDetectResult(ip_address=ip_address)

    # Step 1: SSH banner (most informative)
    banner = await _grab_ssh_banner(ip_address, 22, timeout)
    result.ssh_banner = banner
    result.ssh_port_open = banner is not None

    if banner:
        os_system, os_version = _parse_banner(banner)
        result.os_system = os_system
        result.os_version = os_version

        if os_system in ("linux", "windows", "switch", "router"):
            # ── Credential-based precise probe ──
            # If the banner is generic (e.g. "Linux (OpenSSH) 9.9") and we
            # have login credentials, SSH in and read /etc/os-release to get
            # the exact distro name (e.g. "Rocky Linux 10.0 (Red Quartz)").
            if username and password and os_system == "linux":
                generic_markers = ("linux (openssh)", "linux (dropbear)", "linux")
                if (
                    os_version.lower() in generic_markers
                    or os_version.lower().startswith("linux (openssh)")
                ):
                    precise = await _probe_os_via_ssh(
                        ip_address, username, password, port=ssh_port, timeout=timeout
                    )
                    if precise:
                        result.os_version = precise
                        result.detail = f"通过 SSH 登录识别: {precise}"

            result.confidence = "high"
            result.detail = result.detail or f"通过 SSH Banner 识别: {os_version}"
            return result
        elif os_system == "unknown":
            # SSH responded but we couldn't classify it — still useful signal
            result.confidence = "low"
            result.detail = f"SSH 服务已响应，但无法识别类型: {os_version}"

    # Step 2: Probe RDP and SMB ports in parallel (Windows indicators)
    rdp_task = _tcp_connect(ip_address, 3389, timeout)
    smb_task = _tcp_connect(ip_address, 445, timeout)
    rdp_open, smb_open = await asyncio.gather(rdp_task, smb_task)
    result.rdp_port_open = rdp_open
    result.smb_port_open = smb_open

    # Step 3: Combine signals
    if not banner:
        win_version = ""
        if smb_open:
            # Try to get detailed Windows version via SMB negotiation
            win_version = await _probe_smb_version(ip_address, timeout)

        if rdp_open and smb_open:
            result.os_system = "windows"
            result.os_version = win_version or "Windows (RDP+SMB)"
            result.confidence = "high"
            result.detail = (
                f"通过 RDP (3389) + SMB (445) 端口识别为 {result.os_version}"
            )
        elif rdp_open:
            result.os_system = "windows"
            result.os_version = win_version or "Windows (RDP)"
            result.confidence = "medium"
            result.detail = f"通过 RDP (3389) 端口识别为 {result.os_version}"
        elif smb_open:
            result.os_system = "windows"
            result.os_version = win_version or "Windows (SMB)"
            result.confidence = "medium"
            result.detail = f"通过 SMB (445) 端口识别为 {result.os_version}"
        elif result.os_system in ("unknown", ""):
            result.os_system = "unknown"
            result.confidence = "low"
            result.detail = "无法检测操作系统：SSH/RDP/SMB 端口均未响应"

    return result
