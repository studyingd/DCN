"""
Inspection command registry — command templates per target type and vendor.

Target types: network (5 vendors), linux, windows.
Each entry: (command_string, timeout_seconds)
"""

from app.models.device import Device

# ── Network device commands (5 vendors x 13 items) ──

NETWORK_COMMANDS: dict[str, dict[str, tuple[str, int]]] = {
    "huawei": {
        "cpu": ("display cpu-usage", 15),
        "memory": ("display memory-usage", 15),
        "interface": ("display interface brief", 15),
        "version": ("display version", 15),
        "routes": ("display ip routing-table", 20),
        "log": ("display logbuffer", 15),
        "environment": ("display environment", 10),
        "power": ("display power", 10),
        "fan": ("display fan", 10),
        "stp": ("display stp brief", 15),
        "vlan": ("display vlan", 15),
        "arp": ("display arp", 15),
        "mac": ("display mac-address", 15),
    },
    "cisco": {
        "cpu": ("show processes cpu | include CPU utilization", 15),
        "memory": ("show memory statistics", 15),
        "interface": ("show ip interface brief", 15),
        "version": ("show version", 15),
        "routes": ("show ip route", 20),
        "log": ("show logging", 15),
        "environment": ("show environment all", 10),
        "power": ("show power", 10),
        "fan": ("show environment fan", 10),
        "stp": ("show spanning-tree brief", 15),
        "vlan": ("show vlan brief", 15),
        "arp": ("show ip arp", 15),
        "mac": ("show mac address-table", 15),
    },
    "h3c": {
        "cpu": ("display cpu-usage", 15),
        "memory": ("display memory-usage", 15),
        "interface": ("display interface brief", 15),
        "version": ("display version", 15),
        "routes": ("display ip routing-table", 20),
        "log": ("display logbuffer", 15),
        "environment": ("display environment", 10),
        "power": ("display power", 10),
        "fan": ("display fan", 10),
        "stp": ("display stp brief", 15),
        "vlan": ("display vlan all", 15),
        "arp": ("display arp", 15),
        "mac": ("display mac-address", 15),
    },
    "ruijie": {
        "cpu": ("show cpu", 15),
        "memory": ("show memory", 15),
        "interface": ("show interfaces status", 15),
        "version": ("show version", 15),
        "routes": ("show ip route", 20),
        "log": ("show logging", 15),
        "environment": ("show environment", 10),
        "power": ("show power", 10),
        "fan": ("show fan", 10),
        "stp": ("show spanning-tree", 15),
        "vlan": ("show vlan", 15),
        "arp": ("show arp", 15),
        "mac": ("show mac-address-table", 15),
    },
    "zte": {
        "cpu": ("show cpu", 15),
        "memory": ("show memory", 15),
        "interface": ("show interface brief", 15),
        "version": ("show version", 15),
        "routes": ("show ip route", 20),
        "log": ("show log", 15),
        "environment": ("show environment", 10),
        "power": ("show power", 10),
        "fan": ("show fan", 10),
        "stp": ("show spanning-tree", 15),
        "vlan": ("show vlan", 15),
        "arp": ("show arp", 15),
        "mac": ("show mac address-table", 15),
    },
}

# ── Linux server commands (13 items) ──

LINUX_COMMANDS: dict[str, tuple[str, int]] = {
    "cpu": ("top -bn1 | head -5", 15),
    "memory": ("free -m", 10),
    "disk": ("df -h", 10),
    "load": ("uptime", 10),
    "network": ("ip addr", 10),
    "ports": ("ss -tlnp", 10),
    "processes": ("ps aux --sort=-%mem | head -11", 15),
    "os_version": ("cat /etc/os-release", 10),
    "logs": ('journalctl -p err --since "24 hours ago" | tail -20', 15),
    "failed_services": ("systemctl --failed", 10),
    "firewall": (
        "iptables -L -n 2>/dev/null || ufw status 2>/dev/null || echo 'no firewall found'",
        10,
    ),
    "security_updates": (
        "apt list --upgradable 2>/dev/null || yum check-update --quiet 2>/dev/null || echo 'no package manager'",
        30,
    ),
    "logins": ("last -n 20", 10),
}

# ── Windows server commands (13 items, PowerShell via SSH) ──

WINDOWS_COMMANDS: dict[str, tuple[str, int]] = {
    "cpu": (
        r"powershell -Command \"Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 2 -MaxSamples 1 | Select-Object -ExpandProperty CounterSamples | Select-Object -ExpandProperty CookedValue\"",
        30,
    ),
    "memory": (
        r"powershell -Command \"$os=Get-CimInstance Win32_OperatingSystem; Write-Output ('{0:N1}' -f (($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/$os.TotalVisibleMemorySize*100))\"",
        20,
    ),
    "disk": (
        r"powershell -Command \"Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Format-Table DeviceID,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}},@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,1)}},@{N='UsagePct';E={[math]::Round(($_.Size-$_.FreeSpace)/$_.Size*100,1)}} -AutoSize\"",
        20,
    ),
    "system_info": (
        "systeminfo",
        30,
    ),
    "services": (
        r"powershell -Command \"Get-Service | Where-Object {$_.StartType -eq 'Automatic' -and $_.Status -ne 'Running'} | Format-Table Name,DisplayName,Status -AutoSize\"",
        20,
    ),
    "event_logs": (
        r"powershell -Command \"Get-EventLog -LogName System -EntryType Error -Newest 20 | Format-Table TimeGenerated,Source,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(80,$_.Message.Length))}} -Wrap\"",
        20,
    ),
    "network": (
        "ipconfig /all",
        15,
    ),
    "ports": (
        "netstat -ano | findstr LISTENING",
        15,
    ),
    "processes": (
        r"powershell -Command \"Get-Process | Sort-Object WS -Descending | Select-Object -First 10 Name,Id,CPU,@{N='MB';E={[math]::Round($_.WS/1MB,1)}} | Format-Table -AutoSize\"",
        20,
    ),
    "updates": (
        r"powershell -Command \"Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 10 HotFixID,Description,InstalledOn | Format-Table -AutoSize\"",
        20,
    ),
    "firewall": (
        "netsh advfirewall show allprofiles state",
        15,
    ),
    "uptime": (
        r"powershell -Command \"$bt=(gcim Win32_OperatingSystem).LastBootUpTime; (Get-Date)-$bt | Select Days,Hours,Minutes\"",
        15,
    ),
    "logins": (
        r"powershell -Command \"Get-EventLog -LogName Security -InstanceID 4624 -Newest 10 | Format-Table TimeGenerated,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(100,$_.Message.Length))}} -Wrap\"",
        20,
    ),
}

# ── Quick mode items (top 5 critical checks) ──

QUICK_ITEMS: dict[str, list[str]] = {
    "network": ["cpu", "memory", "interface", "version", "log"],
    "linux": ["cpu", "memory", "disk", "failed_services", "os_version"],
    "windows": ["cpu", "memory", "disk", "services", "system_info"],
}

# ── Item labels (Chinese) for UI ──

ITEM_LABELS: dict[str, str] = {
    "cpu": "CPU 使用率",
    "memory": "内存使用",
    "interface": "接口状态",
    "version": "系统版本",
    "routes": "路由表",
    "log": "系统日志",
    "environment": "环境温度",
    "power": "电源状态",
    "fan": "风扇状态",
    "stp": "STP 状态",
    "vlan": "VLAN 信息",
    "arp": "ARP 表",
    "mac": "MAC 地址表",
    "disk": "磁盘使用",
    "load": "系统负载",
    "network": "网络配置",
    "ports": "监听端口",
    "processes": "进程列表",
    "os_version": "系统版本",
    "logs": "异常日志",
    "failed_services": "失败服务",
    "firewall": "防火墙状态",
    "security_updates": "安全更新",
    "logins": "登录记录",
    "system_info": "系统信息",
    "services": "失败服务",
    "event_logs": "事件日志",
    "updates": "已装补丁",
    "uptime": "运行时间",
}


def get_target_type(device: Device) -> str:
    """Determine inspection target type from device model."""
    network_types = {"switch", "router", "firewall"}
    if device.type in network_types:
        return "network"

    os_lower = (device.os_system or "").lower()
    if "windows" in os_lower:
        return "windows"

    # Default server/host to linux
    return "linux"


def get_commands(
    target_type: str,
    vendor: str | None,
    mode: str,
    custom_items: list[str] | None = None,
) -> list[dict]:
    """
    Return list of { item_type, command, timeout } dicts for the given mode.

    For network devices, vendor must be provided.
    """
    # Select command dictionary
    if target_type == "network":
        vendor = vendor or "huawei"
        cmd_dict = NETWORK_COMMANDS.get(vendor, NETWORK_COMMANDS["huawei"])
    elif target_type == "windows":
        cmd_dict = WINDOWS_COMMANDS
    else:
        cmd_dict = LINUX_COMMANDS

    # Select items based on mode
    if mode == "quick":
        items = QUICK_ITEMS.get(target_type, list(cmd_dict.keys())[:5])
    elif mode == "custom" and custom_items:
        items = [i for i in custom_items if i in cmd_dict]
    else:
        items = list(cmd_dict.keys())

    return [
        {
            "item_type": item_type,
            "command": cmd_dict[item_type][0],
            "timeout": cmd_dict[item_type][1],
        }
        for item_type in items
        if item_type in cmd_dict
    ]


def get_item_catalog(target_type: str) -> list[dict]:
    """Return available items with labels for the UI custom-mode checkbox group."""
    if target_type == "network":
        cmd_dict = NETWORK_COMMANDS["huawei"]  # all vendors share same item types
    elif target_type == "windows":
        cmd_dict = WINDOWS_COMMANDS
    else:
        cmd_dict = LINUX_COMMANDS

    quick_set = set(QUICK_ITEMS.get(target_type, []))
    return [
        {
            "type": item_type,
            "label": ITEM_LABELS.get(item_type, item_type),
            "quick": item_type in quick_set,
        }
        for item_type in cmd_dict.keys()
    ]
