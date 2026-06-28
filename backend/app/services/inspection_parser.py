"""
Inspection result parser — parse CLI output per item type and evaluate thresholds.

Each parser returns: { success, value, unit, status, details }
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── Thresholds ──

THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu": {"warning": 70, "critical": 85},
    "memory": {"warning": 75, "critical": 90},
    "disk": {"warning": 80, "critical": 95},
    "interface_down_pct": {"warning": 30, "critical": 50},
    "failed_services": {"warning": 1, "critical": 3},
}


def _evaluate_threshold(item_type: str, value: float) -> str:
    """Return 'normal', 'warning', or 'critical' based on threshold rules."""
    th = THRESHOLDS.get(item_type)
    if not th:
        return "normal"
    if value >= th["critical"]:
        return "critical"
    if value >= th["warning"]:
        return "warning"
    return "normal"


def parse_item(item_type: str, raw_output: str, target_type: str) -> dict:
    """Dispatch to the appropriate parser based on item_type and target_type."""
    parser_map = {
        "cpu": _parse_cpu,
        "memory": _parse_memory,
        "disk": _parse_disk,
        "interface": _parse_interface,
        "version": _parse_version,
        "routes": _parse_routes,
        "log": _parse_log,
        "environment": _parse_environment,
        "power": _parse_power,
        "fan": _parse_fan,
        "stp": _parse_stp,
        "vlan": _parse_vlan,
        "arp": _parse_arp,
        "mac": _parse_mac,
        "load": _parse_load,
        "network": _parse_network,
        "ports": _parse_ports,
        "processes": _parse_processes,
        "os_version": _parse_os_version,
        "logs": _parse_logs,
        "failed_services": _parse_failed_services,
        "services": _parse_failed_services,  # Windows alias
        "firewall": _parse_firewall,
        "security_updates": _parse_security_updates,
        "logins": _parse_logins,
        "system_info": _parse_system_info,
        "event_logs": _parse_event_logs,
        "updates": _parse_updates,
        "uptime": _parse_uptime,
    }
    parser = parser_map.get(item_type, _parse_generic)
    try:
        return parser(raw_output, target_type)
    except Exception as exc:
        logger.debug("Parse error for %s/%s: %s", item_type, target_type, exc)
        return {
            "success": False,
            "value": None,
            "unit": None,
            "status": "error",
            "details": {"error": str(exc)},
        }


# ══════════════════════════════════════════════════════════
# Individual parsers
# ══════════════════════════════════════════════════════════


def _last_percent(raw: str) -> float | None:
    """Return the last numeric token in *raw* that looks like a percentage (0-100).

    Windows Get-Counter / percentage outputs prefix the value with a counter
    path or sample number; the real value is typically the final number.
    Grabbing the first number (the old behavior) picked up the wrong token
    (counter-path digits, error codes, etc.).
    """
    for token in reversed(re.findall(r"\d+(?:\.\d+)?", raw)):
        try:
            val = float(token)
        except ValueError:
            continue
        if 0 <= val <= 100:
            return val
    return None


def _parse_cpu(raw: str, target_type: str) -> dict:
    """Parse CPU usage from various output formats."""
    usage = None

    if target_type == "linux":
        # top -bn1 | head -5 → "%Cpu(s):  5.2 us,  1.3 sy, ..."
        m = re.search(r"%Cpu\(s\):\s+([\d.]+)\s+us", raw)
        if m:
            usage = float(m.group(1))

    elif target_type == "windows":
        usage = _last_percent(raw)

    else:  # network
        # Huawei/H3C: "CPU Usage : 15%" or "cpu-usage : 15%"
        m = re.search(r"(\d+)\s*%", raw)
        if m:
            usage = float(m.group(1))

    if usage is None:
        return {
            "success": False,
            "value": None,
            "unit": "%",
            "status": "error",
            "details": {"raw": raw[:200]},
        }

    status = _evaluate_threshold("cpu", usage)
    return {
        "success": True,
        "value": f"{usage:.1f}",
        "unit": "%",
        "status": status,
        "details": {"usage_pct": round(usage, 1)},
    }


def _parse_memory(raw: str, target_type: str) -> dict:
    """Parse memory usage."""
    usage = None
    details: dict = {}

    if target_type == "linux":
        # free -m → "Mem:  16384  12288   4096  ..."
        lines = raw.strip().splitlines()
        for line in lines:
            if line.startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 3:
                    total = float(parts[1])
                    used = float(parts[2])
                    usage = (used / total * 100) if total > 0 else 0
                    details = {"total_mb": int(total), "used_mb": int(used)}
                    break

    elif target_type == "windows":
        # Percentage output like "75.3" — take the last plausible percentage.
        usage = _last_percent(raw)

    else:  # network
        m = re.search(r"(\d+)\s*%", raw)
        if m:
            usage = float(m.group(1))

    if usage is None:
        return {
            "success": False,
            "value": None,
            "unit": "%",
            "status": "error",
            "details": {"raw": raw[:200]},
        }

    status = _evaluate_threshold("memory", usage)
    return {
        "success": True,
        "value": f"{usage:.1f}",
        "unit": "%",
        "status": status,
        "details": {"usage_pct": round(usage, 1), **details},
    }


def _parse_disk(raw: str, target_type: str) -> dict:
    """Parse disk usage (Linux/Windows)."""
    filesystems = []
    max_usage = 0.0

    if target_type == "linux":
        # df -h → "/dev/sda1  100G   82G   18G  82%  /"
        for line in raw.strip().splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 6:
                try:
                    pct = float(parts[4].rstrip("%"))
                    filesystems.append(
                        {
                            "device": parts[0],
                            "mount": parts[5],
                            "size": parts[1],
                            "used": parts[2],
                            "avail": parts[3],
                            "usage_pct": pct,
                        }
                    )
                    max_usage = max(max_usage, pct)
                except (ValueError, IndexError):
                    continue

    elif target_type == "windows":
        # Format-Table output with SizeGB, FreeGB, UsagePct
        for line in raw.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                try:
                    device_id = parts[0]
                    size_gb = float(parts[1])
                    free_gb = float(parts[2])
                    pct = float(parts[3])
                    filesystems.append(
                        {
                            "device": device_id,
                            "size_gb": size_gb,
                            "free_gb": free_gb,
                            "usage_pct": pct,
                        }
                    )
                    max_usage = max(max_usage, pct)
                except (ValueError, IndexError):
                    continue

    if not filesystems:
        return {
            "success": False,
            "value": None,
            "unit": "%",
            "status": "error",
            "details": {"raw": raw[:200]},
        }

    status = _evaluate_threshold("disk", max_usage)
    return {
        "success": True,
        "value": f"{max_usage:.1f}",
        "unit": "%",
        "status": status,
        "details": {"max_usage_pct": round(max_usage, 1), "filesystems": filesystems},
    }


def _parse_interface(raw: str, target_type: str) -> dict:
    """Parse interface status table (network devices)."""
    interfaces = []
    up_count = 0
    down_count = 0

    for line in raw.strip().splitlines():
        tokens = line.split()
        if not tokens:
            continue
        name = tokens[0]
        # Skip header rows / non-interface lines (Cisco/Huawei/H3C banners).
        if name.lower() in (
            "interface",
            "interface:",
            "phy",
            "physical",
            "status",
            "protocol",
        ):
            continue

        is_up: bool | None = None
        lowered = [t.lower() for t in tokens]
        # Huawei/H3C "display interface brief": 2nd column is the PHY status
        # (sometimes prefixed with '*' for error-disabled).
        if len(tokens) >= 2 and lowered[1].lstrip("*") in ("up", "down"):
            is_up = lowered[1].lstrip("*") == "up"
        else:
            # Cisco "show ip interface brief": Status/Protocol are the last cols.
            tail = lowered[-2:]
            if "up" in tail or "down" in tail or "administratively" in tail:
                is_up = "up" in tail and "administratively" not in tail

        if is_up is None:
            continue
        if is_up:
            up_count += 1
        else:
            down_count += 1
        interfaces.append({"name": name, "status": "up" if is_up else "down"})

    total = up_count + down_count
    if total == 0:
        return {
            "success": False,
            "value": None,
            "unit": "count",
            "status": "error",
            "details": {"raw": raw[:200]},
        }

    down_pct = (down_count / total) * 100
    status = _evaluate_threshold("interface_down_pct", down_pct)
    return {
        "success": True,
        "value": f"{up_count}/{total}",
        "unit": "up/total",
        "status": status,
        "details": {
            "total": total,
            "up": up_count,
            "down": down_count,
            "down_pct": round(down_pct, 1),
            "interfaces": interfaces[:20],  # cap list size
        },
    }


def parse_interface_brief(raw: str) -> dict:
    """
    Parse network device interface table — NO cap on interface count.

    Used by live interface-status polling for the rack U-view.
    Returns: { success, total, up, down, interfaces: [{name, status}] }
    """
    interfaces = []
    up_count = 0
    down_count = 0

    for line in raw.strip().splitlines():
        m = re.match(r"^(\S+)\s+(UP|up|DOWN|down|ADM|admin)", line, re.IGNORECASE)
        if m:
            name = m.group(1)
            status_str = m.group(2).upper()
            is_up = status_str in ("UP",)
            if is_up:
                up_count += 1
            else:
                down_count += 1
            interfaces.append({"name": name, "status": "up" if is_up else "down"})

    total = up_count + down_count
    if total == 0:
        return {"success": False, "total": 0, "up": 0, "down": 0, "interfaces": []}

    return {
        "success": True,
        "total": total,
        "up": up_count,
        "down": down_count,
        "interfaces": interfaces,
    }


def parse_linux_links(raw: str) -> dict:
    """
    Parse `ip -o link` output. Each line like:
        2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...
        3: eth1: <BROADCAST,MULTICAST> mtu 1500 ...
    The presence of "UP" inside the angle-bracket flags indicates the interface
    is admin-up; "LOWER_UP" indicates the link is actually connected.
    """
    interfaces = []
    up_count = 0
    down_count = 0

    # Pattern: "<digit>+: +<name>: +<flags>"
    pattern = re.compile(r"^\d+:\s+([^:@\s]+)[^<]*<([^>]*)>")
    for line in raw.strip().splitlines():
        m = pattern.match(line)
        if not m:
            continue
        name = m.group(1)
        flags = m.group(2).upper().split(",")
        # Skip loopback and virtual bridges — these always show UP and aren't "real" NIC ports
        if name.lower() in ("lo",) or name.lower().startswith(
            ("docker", "br-", "veth", "virbr")
        ):
            continue
        is_admin_up = "UP" in flags
        has_link = "LOWER_UP" in flags
        if is_admin_up and has_link:
            status = "up"
            up_count += 1
        else:
            status = "down"
            down_count += 1
        interfaces.append({"name": name, "status": status})

    total = up_count + down_count
    if total == 0:
        return {"success": False, "total": 0, "up": 0, "down": 0, "interfaces": []}

    return {
        "success": True,
        "total": total,
        "up": up_count,
        "down": down_count,
        "interfaces": interfaces,
    }


def parse_windows_adapters(raw: str) -> dict:
    """
    Parse PowerShell `Get-NetAdapter | Select Name,Status | Format-Table -AutoSize`.
    Lines look like:
        Name       Status
        ----       ------
        Ethernet0  Up
        Ethernet1  Disabled
    """
    interfaces = []
    up_count = 0
    down_count = 0
    seen_header = False

    for line in raw.strip().splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("name") and "status" in s.lower():
            seen_header = True
            continue
        if s.startswith("-"):
            continue
        if not seen_header:
            continue
        # Match "<name> <status>" — last whitespace-separated token is status
        parts = s.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name, status_str = parts
        status_upper = status_str.upper()
        if status_upper in ("UP",):
            status = "up"
            up_count += 1
        elif status_upper in ("DOWN", "DISCONNECTED", "NOT CONNECTED"):
            status = "down"
            down_count += 1
        elif status_upper in ("DISABLED", "NOT PRESENT"):
            continue  # skip disabled — not a "real" port we can show
        else:
            status = "down"
            down_count += 1
        interfaces.append({"name": name, "status": status})

    total = up_count + down_count
    if total == 0:
        return {"success": False, "total": 0, "up": 0, "down": 0, "interfaces": []}

    return {
        "success": True,
        "total": total,
        "up": up_count,
        "down": down_count,
        "interfaces": interfaces,
    }


def _parse_version(raw: str, target_type: str) -> dict:
    """Parse system version info."""
    return {
        "success": True,
        "value": raw.split("\n")[0][:80] if raw else "unknown",
        "unit": None,
        "status": "normal",
        "details": {"output": raw[:500]},
    }


def _parse_routes(raw: str, target_type: str) -> dict:
    """Parse routing table — count route entries."""
    count = 0
    for line in raw.strip().splitlines():
        # Match lines starting with IP or routing protocol prefix
        if re.match(r"^\d+\.\d+\.\d+\.\d+", line) or re.match(r"^[OCSRBDEI]\s", line):
            count += 1

    status = "warning" if count == 0 else "normal"
    return {
        "success": True,
        "value": str(count),
        "unit": "条",
        "status": status,
        "details": {"route_count": count},
    }


def _parse_log(raw: str, target_type: str) -> dict:
    """Parse system logs — count error/warning entries."""
    lower = raw.lower()
    error_count = len(re.findall(r"error|critical|alert|emergency", lower))
    warning_count = len(re.findall(r"warning|notice", lower))

    if error_count > 5:
        status = "critical"
    elif error_count > 0 or warning_count > 10:
        status = "warning"
    else:
        status = "normal"

    return {
        "success": True,
        "value": f"{error_count}错误/{warning_count}警告",
        "unit": None,
        "status": status,
        "details": {"error_count": error_count, "warning_count": warning_count},
    }


def _parse_environment(raw: str, target_type: str) -> dict:
    """Parse environment/temperature status."""
    return {
        "success": True,
        "value": raw.split("\n")[0][:80] if raw.strip() else "无数据",
        "unit": None,
        "status": "normal",
        "details": {"output": raw[:500]},
    }


def _parse_power(raw: str, target_type: str) -> dict:
    """Parse power supply status."""
    lower = raw.lower()
    has_abnormal = "fail" in lower or "off" in lower or "fault" in lower
    return {
        "success": True,
        "value": "异常" if has_abnormal else "正常",
        "unit": None,
        "status": "warning" if has_abnormal else "normal",
        "details": {"output": raw[:300]},
    }


def _parse_fan(raw: str, target_type: str) -> dict:
    """Parse fan status."""
    lower = raw.lower()
    has_abnormal = "fail" in lower or "fault" in lower or "abnormal" in lower
    return {
        "success": True,
        "value": "异常" if has_abnormal else "正常",
        "unit": None,
        "status": "warning" if has_abnormal else "normal",
        "details": {"output": raw[:300]},
    }


def _parse_stp(raw: str, target_type: str) -> dict:
    """Parse STP status."""
    return {
        "success": True,
        "value": raw.split("\n")[0][:80] if raw.strip() else "无数据",
        "unit": None,
        "status": "normal",
        "details": {"output": raw[:500]},
    }


def _parse_vlan(raw: str, target_type: str) -> dict:
    """Parse VLAN info — count VLANs."""
    count = 0
    for line in raw.strip().splitlines():
        if re.match(r"^\d+", line):
            count += 1
    return {
        "success": True,
        "value": str(count),
        "unit": "个",
        "status": "normal",
        "details": {"vlan_count": count, "output": raw[:300]},
    }


def _parse_arp(raw: str, target_type: str) -> dict:
    """Parse ARP table — count entries."""
    lines = [l for l in raw.strip().splitlines() if re.match(r"^\d+\.\d+\.\d+\.\d+", l)]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "normal",
        "details": {"arp_count": len(lines)},
    }


def _parse_mac(raw: str, target_type: str) -> dict:
    """Parse MAC address table — count entries."""
    lines = [
        l
        for l in raw.strip().splitlines()
        if re.search(r"[0-9a-fA-F]{4}[-.][0-9a-fA-F]{4}", l)
    ]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "normal",
        "details": {"mac_count": len(lines)},
    }


def _parse_load(raw: str, target_type: str) -> dict:
    """Parse system load average (Linux: uptime)."""
    # "10:30:00 up 5 days, 3:20, 2 users, load average: 0.15, 0.10, 0.08"
    m = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", raw)
    if m:
        load_1 = float(m.group(1))
        load_5 = float(m.group(2))
        load_15 = float(m.group(3))
        return {
            "success": True,
            "value": f"{load_1}",
            "unit": None,
            "status": "warning" if load_1 > 5 else "normal",
            "details": {"load_1": load_1, "load_5": load_5, "load_15": load_15},
        }
    return {
        "success": False,
        "value": None,
        "unit": None,
        "status": "error",
        "details": {"raw": raw[:200]},
    }


def _parse_network(raw: str, target_type: str) -> dict:
    """Parse network config (ip addr / ipconfig)."""
    if target_type == "windows":
        ips = re.findall(r"IPv4 Address[.\s]*:\s*(\d+\.\d+\.\d+\.\d+)", raw)
    else:
        ips = re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)", raw)
    return {
        "success": True,
        "value": ", ".join(ips[:3]) if ips else "无IP",
        "unit": None,
        "status": "normal",
        "details": {"ip_addresses": ips},
    }


def _parse_ports(raw: str, target_type: str) -> dict:
    """Parse listening ports."""
    if target_type == "windows":
        ports = re.findall(r"LISTENING\s+(\d+)", raw)
    else:
        ports = re.findall(r"LISTEN\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+):(\d+)", raw)
    port_list = list(set(ports))[:20] if ports else []
    return {
        "success": True,
        "value": str(len(port_list)),
        "unit": "个",
        "status": "normal",
        "details": {"port_count": len(port_list), "ports": port_list},
    }


def _parse_processes(raw: str, target_type: str) -> dict:
    """Parse top processes (``ps aux``)."""
    procs = []
    for line in raw.strip().splitlines():
        parts = line.split(None, 10)
        # Header is "USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND".
        # Skip the header (which may repeat) and any short/non-conforming line.
        if len(parts) < 11 or parts[0] == "USER":
            continue
        # parts[2]=%CPU, parts[3]=%MEM; parts[10]=command (START/TIME may merge).
        procs.append(
            {
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "command": parts[10][:60],
            }
        )
    if not procs:
        # Empty result means the output didn't match `ps aux` (different ps build,
        # missing header, etc.) — surface an error instead of "0 processes, OK".
        return {
            "success": False,
            "value": None,
            "unit": "个",
            "status": "error",
            "details": {"raw": raw[:200]},
        }
    return {
        "success": True,
        "value": str(len(procs)),
        "unit": "个",
        "status": "normal",
        "details": {"processes": procs[:10]},
    }


def _parse_os_version(raw: str, target_type: str) -> dict:
    """Parse /etc/os-release."""
    name = ""
    version = ""
    for line in raw.strip().splitlines():
        if line.startswith("PRETTY_NAME="):
            name = line.split("=", 1)[1].strip('"')
        elif line.startswith("VERSION="):
            version = line.split("=", 1)[1].strip('"')
    value = name or raw.split("\n")[0][:80]
    return {
        "success": True,
        "value": value,
        "unit": None,
        "status": "normal",
        "details": {"name": name, "version": version},
    }


def _parse_logs(raw: str, target_type: str) -> dict:
    """Parse error logs (journalctl / event_logs)."""
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "warning" if len(lines) > 10 else "normal",
        "details": {"error_count": len(lines), "output": raw[:500]},
    }


def _parse_failed_services(raw: str, target_type: str) -> dict:
    """Parse failed/stopped-auto services."""
    if target_type == "windows":
        # PowerShell Format-Table output
        lines = [
            l
            for l in raw.strip().splitlines()
            if l.strip() and "Name" not in l and "---" not in l
        ]
    else:
        # systemctl --failed → count UNIT lines
        lines = [
            l
            for l in raw.strip().splitlines()
            if l.strip()
            and not l.startswith("UNIT")
            and not l.startswith("●")
            and "units listed" not in l.lower()
        ]

    # Filter actual service lines (not headers/empty)
    svc_lines = [l for l in lines if l.strip() and not all(c in "-\t " for c in l)]
    count = len(svc_lines)

    status = "critical" if count > 0 else "normal"
    return {
        "success": True,
        "value": str(count),
        "unit": "个",
        "status": status,
        "details": {
            "failed_count": count,
            "services": [l.strip()[:80] for l in svc_lines[:10]],
        },
    }


def _parse_firewall(raw: str, target_type: str) -> dict:
    """Parse firewall status."""
    lower = raw.lower()
    if target_type == "windows":
        active = "ON" in raw.upper() or "启用" in raw or "OK" in raw
    else:
        active = (
            "active" in lower
            or "enabled" in lower
            or "running" in lower
            or "status: active" in lower
        )

    no_fw = "no firewall" in lower or "no package manager" in lower
    if no_fw:
        status = "warning"
        value = "未检测到防火墙"
    elif active:
        status = "normal"
        value = "已启用"
    else:
        status = "warning"
        value = "未启用"

    return {
        "success": True,
        "value": value,
        "unit": None,
        "status": status,
        "details": {"active": active, "output": raw[:300]},
    }


def _parse_security_updates(raw: str, target_type: str) -> dict:
    """Parse security updates available."""
    lines = [
        l
        for l in raw.strip().splitlines()
        if l.strip() and "upgradable" not in l.lower() and "listing" not in l.lower()
    ]
    # Filter package lines (contain '/')
    pkg_lines = [l for l in lines if "/" in l]
    count = len(pkg_lines)
    return {
        "success": True,
        "value": str(count),
        "unit": "个",
        "status": "warning" if count > 20 else "normal",
        "details": {
            "update_count": count,
            "packages": [l.strip()[:80] for l in pkg_lines[:10]],
        },
    }


def _parse_logins(raw: str, target_type: str) -> dict:
    """Parse recent logins (output of `last -n N`)."""
    # Count real login records: non-blank lines that are not the `wtmp begins`
    # footer or the `reboot` pseudo-record.
    lines = [
        l
        for l in raw.strip().splitlines()
        if l.strip()
        and "wtmp begins" not in l.lower()
        and not l.lower().startswith("reboot")
    ]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "normal",
        "details": {"login_count": len(lines), "output": raw[:500]},
    }


def _parse_system_info(raw: str, target_type: str) -> dict:
    """Parse Windows systeminfo."""
    os_name = ""
    for line in raw.strip().splitlines():
        if "OS Name" in line or "OS 名称" in line:
            os_name = line.split(":", 1)[1].strip() if ":" in line else ""
            break
    return {
        "success": True,
        "value": os_name or raw.split("\n")[0][:80],
        "unit": None,
        "status": "normal",
        "details": {"output": raw[:500]},
    }


def _parse_event_logs(raw: str, target_type: str) -> dict:
    """Parse Windows event logs."""
    lines = [
        l
        for l in raw.strip().splitlines()
        if l.strip() and "TimeGenerated" not in l and "--" not in l
    ]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "warning" if len(lines) > 5 else "normal",
        "details": {"error_count": len(lines), "output": raw[:500]},
    }


def _parse_updates(raw: str, target_type: str) -> dict:
    """Parse Windows installed updates."""
    lines = [
        l
        for l in raw.strip().splitlines()
        if l.strip() and "HotFixID" not in l and "--" not in l
    ]
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "个",
        "status": "normal",
        "details": {"update_count": len(lines), "output": raw[:300]},
    }


def _parse_uptime(raw: str, target_type: str) -> dict:
    """Parse Windows uptime."""
    days = hours = minutes = 0
    m = re.search(r"(\d+)\s*Days?", raw)
    if m:
        days = int(m.group(1))
    m = re.search(r"(\d+)\s*Hours?", raw)
    if m:
        hours = int(m.group(1))
    m = re.search(r"(\d+)\s*Minutes?", raw)
    if m:
        minutes = int(m.group(1))

    total_hours = days * 24 + hours
    return {
        "success": True,
        "value": f"{days}天{hours}时{minutes}分",
        "unit": None,
        "status": "normal",
        "details": {
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "total_hours": total_hours,
        },
    }


def _parse_generic(raw: str, target_type: str) -> dict:
    """Fallback parser for unknown item types."""
    return {
        "success": True,
        "value": raw.split("\n")[0][:80] if raw.strip() else "无输出",
        "unit": None,
        "status": "normal",
        "details": {"output": raw[:500]},
    }
