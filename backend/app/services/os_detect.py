"""
Operating system detection via SSH banner grabbing and TCP port probing.

Detects OS by connecting to common ports (SSH:22, WinRM:5985, RDP:3389,
SMB:445) and parsing the SSH protocol banner string that every SSH server
sends on connect — no authentication required.

SSH banner format:  SSH-2.0-<software> [comments]
    SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.4  →  Ubuntu Linux
    SSH-2.0-OpenSSH_for_Windows_8.1          →  Windows (版本号属于 OpenSSH,不可当 OS 版本)
    SSH-2.0-dropbear_2022.82                 →  Linux (embedded)

Windows 精确识别走 WinRM(平台不对 Windows 使用 SSH):
    凭据 + WinRM 可达 → Get-CimInstance Win32_OperatingSystem.Caption
    → "Microsoft Windows Server 2019 Standard" 级别的精确名称。
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
    # 注意:OpenSSH_for_Windows 后面的数字是 OpenSSH 版本(7.7/8.1/9.5…),
    # 与 Windows 版本无关——只识别家族,版本留给 WinRM 精确探测。
    # 平台不纳管交换机/路由器,因此不再匹配 Cisco/Juniper/Huawei/ROS 等网络设备
    # banner,这类 banner 统一落到 "unknown" 分支。
    (re.compile(r"OpenSSH_for_Windows", re.I), "windows", "Windows"),
    (re.compile(r"Dropbear[_\s](\S+)", re.I), "linux", "Linux (Dropbear)"),
    (re.compile(r"VMware[-\s](\S+)", re.I), "server", "VMware ESXi"),
    (re.compile(r"ESXi[-\s](\S+)", re.I), "server", "VMware ESXi"),
    # ── Generic OpenSSH (no OS comment → assume Linux) ──
    (re.compile(r"OpenSSH[_\s](\S+)", re.I), "linux", "Linux (OpenSSH)"),
]


@dataclass
class OSDetectResult:
    """Result of OS detection probe."""

    ip_address: str
    os_system: str = ""  # "linux" / "windows" / "unknown"
    os_version: str = ""  # human-readable version hint e.g. "Ubuntu 22.04"
    ssh_banner: str | None = None  # raw SSH banner string
    ssh_port_open: bool = False
    winrm_port_open: bool = False
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
            "winrm_port_open": self.winrm_port_open,
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


def probe_linux_os_via_ssh_sync(
    ip_address: str,
    username: str,
    password: str,
    port: int = 22,
    timeout: float = 8.0,
    pinned_key: str | None = None,
    private_key: str | None = None,
) -> str | None:
    """SSH 登录读取 /etc/os-release,拿精确发行版名(Linux 专用)。同步版本。

    支持密码或私钥(运维接入允许只配私钥)。
    Returns the pretty OS name (e.g. "Rocky Linux 10.0 (Red Quartz)")
    or ``None`` if the probe fails.
    """
    from app.services.ssh import exec_ssh_command, open_ssh_client

    try:
        client, _key = open_ssh_client(
            ip_address,
            port,
            username,
            password,
            timeout=int(timeout),
            pinned_key_b64=pinned_key,
            private_key=private_key,
            allow_tofu=not bool(pinned_key),
        )
    except Exception:
        return None
    try:
        _exit, output, _err = exec_ssh_command(
            client, "cat /etc/os-release 2>/dev/null", timeout=int(timeout)
        )
        pretty_name = ""
        version_id = ""
        name = ""
        for line in output.splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty_name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("VERSION_ID="):
                version_id = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("NAME="):
                name = line.split("=", 1)[1].strip().strip('"')

        if pretty_name:
            return pretty_name
        if name:
            return f"{name} {version_id}" if version_id else name
        return None
    except Exception:
        return None
    finally:
        client.close()


async def _probe_linux_os_via_ssh(
    ip_address: str,
    username: str,
    password: str,
    port: int = 22,
    timeout: float = 8.0,
    pinned_key: str | None = None,
    private_key: str | None = None,
) -> str | None:
    """异步包装:probe_linux_os_via_ssh_sync 的线程池版本。"""
    return await asyncio.to_thread(
        probe_linux_os_via_ssh_sync,
        ip_address,
        username,
        password,
        port,
        timeout,
        pinned_key,
        private_key,
    )


def probe_windows_os_via_winrm_sync(
    ip_address: str,
    username: str,
    password: str,
    port: int,
    timeout: float = 8.0,
) -> str | None:
    """经 WinRM 查询 Win32_OperatingSystem.Caption,拿精确 Windows 版本名。同步版本。

    Returns e.g. "Microsoft Windows Server 2019 Standard",or ``None``
    if the probe fails.
    """
    from app.services.winrm import WinRMError, run_powershell

    script = "$o = Get-CimInstance Win32_OperatingSystem; Write-Output $o.Caption"

    try:
        code, out, _err = run_powershell(
            ip_address,
            username,
            password,
            script,
            port=port,
            timeout=int(timeout),
        )
    except WinRMError:
        return None
    if code != 0 or not out:
        return None
    caption = out.splitlines()[0].strip()
    return caption or None


async def _probe_os_via_winrm(
    ip_address: str,
    username: str,
    password: str,
    port: int,
    timeout: float = 8.0,
) -> str | None:
    """异步包装:probe_windows_os_via_winrm_sync 的线程池版本。"""
    return await asyncio.to_thread(
        probe_windows_os_via_winrm_sync, ip_address, username, password, port, timeout
    )


async def detect_os(
    ip_address: str,
    timeout: float = DEFAULT_TIMEOUT,
    username: str | None = None,
    password: str | None = None,
    ssh_port: int = 22,
    winrm_port: int | None = None,
    ssh_key: str | None = None,
) -> OSDetectResult:
    """Probe an IP address to detect its operating system.

    Strategy:
      1. Grab SSH banner → parse for OS hints(Linux 可带凭据读 os-release 精确识别)。
      2. Probe WinRM / RDP / SMB as Windows indicators;有凭据且 WinRM 可达时
         查 Win32_OperatingSystem.Caption 精确识别(如 "Windows Server 2019 Standard")。
      3. Combine all signals into a confidence score.
    """
    from app.config import WINRM_PORT

    result = OSDetectResult(ip_address=ip_address)
    winrm_port = winrm_port or WINRM_PORT

    # Step 1: SSH banner (most informative for Linux / network devices)
    banner = await _grab_ssh_banner(ip_address, ssh_port, timeout)
    result.ssh_banner = banner
    result.ssh_port_open = banner is not None

    if banner:
        os_system, os_version = _parse_banner(banner)
        result.os_system = os_system
        result.os_version = os_version

        if os_system == "linux":
            # ── Credential-based precise probe ──
            # Banner 只有笼统的 "Linux (OpenSSH) 9.9" 时,登录读 /etc/os-release
            # 拿精确发行版名(如 "Rocky Linux 10.0 (Red Quartz)")。
            if username and (password or ssh_key):
                generic_markers = ("linux (openssh)", "linux (dropbear)", "linux")
                if any(
                    os_version.lower().startswith(marker) for marker in generic_markers
                ):
                    precise = await _probe_linux_os_via_ssh(
                        ip_address,
                        username,
                        password or "",
                        port=ssh_port,
                        timeout=timeout,
                        private_key=ssh_key,
                    )
                    if precise:
                        result.os_version = precise
                        result.detail = f"通过 SSH 登录识别: {precise}"

            result.confidence = "high"
            result.detail = result.detail or f"通过 SSH Banner 识别: {os_version}"
            return result

        if os_system == "windows":
            # Windows 精确版本依赖 WinRM 探测——但 OpenSSH_for_Windows 场景本就
            # 罕见(平台约定 Windows 不装 SSH),此处按家族高置信返回即可。
            result.confidence = "high"
            result.detail = f"通过 SSH Banner 识别: {os_version}"
            return result

        if os_system == "unknown":
            # SSH responded but we couldn't classify it — still useful signal
            result.confidence = "low"
            result.detail = f"SSH 服务已响应,但无法识别类型: {os_version}"

    # Step 2: Probe Windows indicator ports in parallel
    winrm_task = _tcp_connect(ip_address, winrm_port, timeout)
    rdp_task = _tcp_connect(ip_address, 3389, timeout)
    smb_task = _tcp_connect(ip_address, 445, timeout)
    winrm_open, rdp_open, smb_open = await asyncio.gather(
        winrm_task, rdp_task, smb_task
    )
    result.winrm_port_open = winrm_open
    result.rdp_port_open = rdp_open
    result.smb_port_open = smb_open

    # Step 3: 有凭据且 WinRM 可达 → Caption 精确识别(最权威)
    if winrm_open and username and password:
        precise = await _probe_os_via_winrm(
            ip_address, username, password, winrm_port, timeout=8.0
        )
        if precise:
            result.os_system = "windows"
            result.os_version = precise
            result.confidence = "high"
            result.detail = f"通过 WinRM 登录识别: {precise}"
            return result

    # Step 4: Combine port signals (weak identification)
    if winrm_open or rdp_open or smb_open:
        win_version = ""
        if smb_open:
            # Try to get a coarse Windows version via SMB negotiation
            win_version = await _probe_smb_version(ip_address, timeout)

        signals = []
        if winrm_open:
            signals.append(f"WinRM ({winrm_port})")
        if rdp_open:
            signals.append("RDP (3389)")
        if smb_open:
            signals.append("SMB (445)")

        result.os_system = "windows"
        result.os_version = win_version or "Windows"
        result.confidence = (
            "high" if (winrm_open or (rdp_open and smb_open)) else "medium"
        )
        result.detail = f"通过 {' + '.join(signals)} 端口识别为 {result.os_version}"
    elif result.os_system in ("unknown", ""):
        result.os_system = "unknown"
        result.confidence = "low"
        result.detail = "无法检测操作系统:SSH/WinRM/RDP/SMB 端口均未响应"

    return result
