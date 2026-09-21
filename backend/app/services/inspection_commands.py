"""
Inspection command registry — command templates per target type.

Target types: linux, windows.
Each entry: (command_string, timeout_seconds)
"""

from app.models.device import Device

# ── Linux server commands (13 items) ──

LINUX_COMMANDS: dict[str, tuple[str, int]] = {
    # cpu/memory/load 的输出会被 inspection_parser 按英文关键字解析
    # （"%Cpu(s):" / "Mem:" / "load average:"）。中文 locale 下这些标签会
    # 变成「%Cpu(s)：」「内存：」「平均负载：」（全角冒号），解析直接失败 →
    # 巡检项误报「错误」。统一前置 LC_ALL=C 强制英文输出，与 locale 无关。
    "cpu": ("LC_ALL=C top -bn1 | head -5", 15),
    "memory": ("LC_ALL=C free -m", 10),
    # df 是位置解析，与 locale 无关，不需要 LC_ALL=C。但必须排除伪文件系统：
    # efivarfs（固件变量，256K 里常年用 90%+）、tmpfs/devtmpfs（内存盘，占满
    # 由 memory 项反映）、overlay（容器 rootfs，用量与底层 / 重复）、squashfs
    # （snap 只读挂载）。不排除的话 max_usage 被伪文件系统顶高，真实磁盘
    # 才 14% 也会误报磁盘警告（实测）。表头是中文不受影响——解析不看表头。
    "disk": (
        "df -h -x tmpfs -x devtmpfs -x efivarfs -x overlay -x squashfs",
        10,
    ),
    "load": ("LC_ALL=C uptime", 10),
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

# ── Windows「服务异常」检查项 ──
# 不再枚举「StartType=Automatic 且没在运行」的服务——那是在数噪音：
#   * Get-Service 分不出「自动(延迟启动)」，.NET 的 ServiceStartMode 根本没这一档，
#     于是 sppsvc / wuauserv 这类干完活就自己退出的服务永远在列；
#   * RemoteRegistry、spice-agent 本来就是「应该停着」的，前者开着才是安全风险。
# 一台健康虚拟机随随便便凑齐 3 个，直接被阈值判成 critical。
#
# 改成查系统日志里 Service Control Manager 的失败事件：这才是 Windows 自己
# 认定的「服务出问题了」，与语言无关（走 ReplacementStrings，不走已本地化的 Message）。
WINDOWS_SERVICE_FAILURE_WINDOW_HOURS = 24

# SCM 失败类事件 ID → 语义。7036(服务进入运行/停止状态)等正常事件不在此列。
SCM_FAILURE_EVENT_IDS: dict[int, str] = {
    7000: "启动失败",
    7001: "因依赖服务失败而未能启动",
    7009: "启动连接超时",
    7011: "事务响应超时",
    7023: "以错误终止",
    7024: "以服务特定错误终止",
    7031: "意外终止(已按恢复策略处置)",
    7034: "意外终止",
}

# 7009 / 7011 的 ReplacementStrings 是 (超时毫秒数, 服务名)，其余事件服务名在 [0]。
_SCM_SERVICE_NAME_AT_INDEX_1 = (7009, 7011)

# 输出契约(见 inspection_parser._windows_scm_failures)：每行
#   发生次数|事件ID|服务名
# 先按「事件ID|服务名」聚合，所以同一个服务反复崩溃只占一行、次数累加，
# 不会因为崩溃循环把计数刷爆。
#
# 这条命令是「Windows 上什么算服务故障」的唯一真源，**健康巡检与 Agent 诊断
# 共用**（agent_commands.WINDOWS_DIAGNOSTICS["failed_services"] 直接引用它）。
# 两边必须给出同一份事实，否则 Agent 报告与巡检结论会互相矛盾；而且它注册进
# Agent 时会再过一次 assert_readonly 的只读校验，等于多一道安全网。
WINDOWS_SCM_FAILURE_COMMAND = (
    "$ids=@(" + ",".join(str(i) for i in sorted(SCM_FAILURE_EVENT_IDS)) + "); "
    "Get-EventLog -LogName System -Source 'Service Control Manager' "
    "-EntryType Error,Warning "
    "-After (Get-Date).AddHours(-"
    + str(WINDOWS_SERVICE_FAILURE_WINDOW_HOURS)
    + ") -Newest 200 -ErrorAction SilentlyContinue | "
    "Where-Object { $ids -contains $_.InstanceId } | "
    "ForEach-Object { $n = if ($_.InstanceId -eq "
    + str(_SCM_SERVICE_NAME_AT_INDEX_1[0])
    + " -or $_.InstanceId -eq "
    + str(_SCM_SERVICE_NAME_AT_INDEX_1[1])
    + ") { $_.ReplacementStrings[1] } else { $_.ReplacementStrings[0] }; "
    "('{0}|{1}' -f $_.InstanceId,$n) } | "
    "Group-Object | "
    # 管道尾部显式 exit 0:Get-EventLog 在「无匹配事件」时即使带
    # -ErrorAction SilentlyContinue 也会把退出码置 1(SilentlyContinue 只压
    # 错误输出,不影响 $LASTEXITCODE/进程退出码)。巡检对 exit!=0 一律按
    # 命令失败处理(不喂解析器),于是健康的机器(24h 无服务失败事件)反而
    # 显示「服务异常:命令退出码 1」的 error 项。exit 0 后无输出经解析器
    # 计为 0 个失败服务,即健康结论。
    "ForEach-Object { ('{0}|{1}' -f $_.Count,$_.Name) }; exit 0"
)

# ── Windows server commands (13 items, PowerShell via WinRM run_ps) ──
# 纯 PowerShell 脚本,直接喂给 pywinrm run_ps——不再包 powershell -Command
# 转义壳(平台约定 Windows 设备不装 SSH)。cpu 项用煮熟计数器瞬时直读,
# 替代 Get-Counter 的 2 秒采样。

WINDOWS_COMMANDS: dict[str, tuple[str, int]] = {
    "cpu": (
        "(Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\").PercentProcessorTime",
        15,
    ),
    "memory": (
        "$os=Get-CimInstance Win32_OperatingSystem; '{0:N1}' -f (($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/$os.TotalVisibleMemorySize*100)",
        15,
    ),
    "disk": (
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Format-Table DeviceID,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}},@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,1)}},@{N='UsagePct';E={[math]::Round(($_.Size-$_.FreeSpace)/$_.Size*100,1)}} -AutoSize",
        15,
    ),
    "system_info": (
        "systeminfo",
        30,
    ),
    "services": (
        WINDOWS_SCM_FAILURE_COMMAND,
        25,
    ),
    # 输出契约(见 inspection_parser._parse_event_logs)：每行
    #   时间|来源|事件ID|摘要(空白压平、截断 120 字符)
    # 判定口径（系统性防误报，不再靠逐个补噪声表）：
    #   1. 只看最近 24 小时——上周的老账不算今天的健康问题，与 services 项同窗口；
    #   2. 解析器按「来源+事件ID」聚合，同一类事件爆发 40 次只算 1 类，
    #      未知新型噪音落进聚合层自然超不过阈值，无需逐个收编；
    #   3. InstanceId 让解析器能额外剔除「每天都会复发的慢性噪音」
    #      (TPM-WMI 1801 / Schannel 36874 / DCOM 10010 等)。
    # Newest 200 是补偿过滤与聚合消耗。
    "event_logs": (
        "Get-EventLog -LogName System -EntryType Error "
        "-After (Get-Date).AddHours(-24) -Newest 200 -ErrorAction SilentlyContinue | "
        "ForEach-Object { $m = ($_.Message -replace '\\s+', ' '); "
        "if ($m.Length -gt 120) { $m = $m.Substring(0, 120) }; "
        "('{0:yyyy-MM-dd HH:mm:ss}|{1}|{2}|{3}' -f $_.TimeGenerated, $_.Source, $_.InstanceId, $m) }",
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
        "Get-Process | Sort-Object WS -Descending | Select-Object -First 10 Name,Id,CPU,@{N='MB';E={[math]::Round($_.WS/1MB,1)}} | Format-Table -AutoSize",
        20,
    ),
    "updates": (
        "Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 10 HotFixID,Description,InstalledOn | Format-Table -AutoSize",
        20,
    ),
    "firewall": (
        "netsh advfirewall show allprofiles state",
        15,
    ),
    "uptime": (
        "$bt=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; (Get-Date)-$bt | Select-Object Days,Hours,Minutes",
        15,
    ),
    "logins": (
        "Get-EventLog -LogName Security -InstanceID 4624 -Newest 10 | Format-Table TimeGenerated,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(100,$_.Message.Length))}} -Wrap",
        20,
    ),
}

# ── Quick mode items (top 5 critical checks) ──

QUICK_ITEMS: dict[str, list[str]] = {
    "linux": ["cpu", "memory", "disk", "failed_services", "os_version"],
    "windows": ["cpu", "memory", "disk", "services", "system_info"],
}

# ── 核心指标模式（自动化运维的健康巡检固定跑这一组，不再让用户挑模式）──
# 规模取 quick(5 项) 与 standard(全量 13 项) 之间的平均值：每个 target_type 恰好 6 项，
# 结构都是「4 项性能 + 2 项健康」。
#   linux   —— cpu / memory / disk / load + failed_services / logs
#   windows —— 没有 load 和 logs，按语义对齐替代：
#              uptime↔load、services↔failed_services、event_logs↔logs

CORE_ITEMS: dict[str, list[str]] = {
    "linux": ["cpu", "memory", "disk", "load", "failed_services", "logs"],
    "windows": ["cpu", "memory", "disk", "uptime", "services", "event_logs"],
}

# ── Item labels (Chinese) for UI ──

ITEM_LABELS: dict[str, str] = {
    "cpu": "CPU 使用率",
    "memory": "内存使用",
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
    "services": "服务异常",
    "event_logs": "事件日志",
    "updates": "已装补丁",
    "uptime": "运行时间",
}


def get_target_type(device: Device) -> str:
    """Determine inspection target type from device model."""
    os_lower = (device.os_system or "").lower()
    if "windows" in os_lower:
        return "windows"

    # Default server/host to linux
    return "linux"


def get_commands(
    target_type: str,
    mode: str,
    custom_items: list[str] | None = None,
) -> list[dict]:
    """
    Return list of { item_type, command, timeout } dicts for the given mode.
    """
    # Select command dictionary
    if target_type == "windows":
        cmd_dict = WINDOWS_COMMANDS
    else:
        cmd_dict = LINUX_COMMANDS

    # Select items based on mode
    if mode == "quick":
        items = QUICK_ITEMS.get(target_type, list(cmd_dict.keys())[:5])
    elif mode == "core":
        # 核心指标固定集；未登记的 target_type 退回注册表前 6 项，保持规模一致。
        items = CORE_ITEMS.get(target_type, list(cmd_dict.keys())[:6])
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
    if target_type == "windows":
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
