"""Agent 只读诊断命令注册表——Agent 能力的唯一可执行面。

边界设计(硬约束,按重要性排序):
  1. LLM 只能通过 key "点名"本注册表中的条目执行,**永远不接触 shell 自由输入**;
     服务端对 key 做强制白名单校验,幻觉出的 key 直接拒绝。
  2. 本模块在**导入期**对每条命令做只读校验(assert_readonly),写入/删除/修改类
     原语(重定向、rm/Remove-Item、写操作动词、命令替换等)会导致模块加载失败——
     配合 tests/test_agent_commands.py,没人能往注册表里误加写命令。
  3. 每次诊断执行的条目、原始输出、最终报告全部落库审计(agent_runs 表)。

注意:命令仍以设备绑定的特权凭据执行,OS 层面并非只读账户。这里的边界在
"Agent 可触达的操作面";如需纵深防御,后续可为设备配置只读系统账户。
"""

from __future__ import annotations

import re

from app.models.device import Device
from app.services.inspection_commands import (
    WINDOWS_SCM_FAILURE_COMMAND,
    WINDOWS_SERVICE_FAILURE_WINDOW_HOURS,
)

# ══════════════════════════════════════════════════════════════════════
# 只读校验(注册期调用;单测覆盖)
# ══════════════════════════════════════════════════════════════════════

# shell 写入/破坏类动词与原语
_SHELL_WRITE_PATTERNS = [
    r"\b(?:rm|mv|cp|dd|mkfs(?:\.\w+)?|tee|truncate|shred|mkdir|touch|chmod|chown|chgrp|ln|sync)\b",
    r"\b(?:useradd|userdel|usermod|groupadd|groupdel|passwd|crontab|visudo)\b",
    r"\b(?:mount|umount|fdisk|parted|losetup|swapoff|mkswap)\b",
    r"\b(?:reboot|shutdown|halt|poweroff|init|kill|pkill|killall|skill)\b",
    r"\bsystemctl\s+[^|]*\b(?:start|stop|restart|reload|enable|disable|mask|unmask|edit)\b",
    r"\bsed\b[^|]*\s-i\b",  # sed -i 就地改文件
    r"\bapt(?:-get)?\b[^|]*\b(?:install|remove|purge|upgrade|dist-upgrade)\b",
    r"\b(?:yum|dnf|zypper)\b[^|]*\b(?:install|remove|erase|update|upgrade)\b",
    r"\bpip3?\b[^|]*\b(?:install|uninstall)\b",
    r"\biptables\s+-(?!L\b|S\b)\w",  # 只允许 iptables -L/-S(读)
    r"\bnft\s+(?!list\b)\w+",  # 只允许 nft list(读)
    r"\bufw\s+(?!status\b)\w+",  # 只允许 ufw status(读)
    r"\bfirewall-cmd\s+--(?!list|get|check|query)\w+",
]

# PowerShell 写入/破坏类动词(Format-Table/Write-Output 显式豁免)
_PS_WRITE_PATTERNS = [
    r"\b(?:Set|New|Remove|Stop|Start|Restart|Clear|Enable|Disable|Register|Unregister|Add|Rename|Copy|Move|Install|Uninstall|Kill|Send|Mount|Dismount)-\w+",
    r"\bFormat-(?!Table\b)\w+",  # 只允许 Format-Table
    r"\bWrite-(?!Output\b)\w+",  # 只允许 Write-Output
    r"\b(?:Out-File|Set-Content|Add-Content|Clear-Content)\b",
    r"\b(?:iex|Invoke-Expression|Invoke-Command|Invoke-WebRequest|iwr|curl|wget)\b",
    r"\b(?:del|erase|rd|rmdir|shutdown|diskpart|takeown|icacls)\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\breg\s+(?:add|delete|import)\b",
    r"\bnet\s+(?:user|localgroup|share)\b",
]


def _has_illegal_redirect(command: str) -> bool:
    """任何 > / >> 重定向都必须指向 /dev/null 或 fd 复制(&1/&2)——其余即写文件。"""
    for m in re.finditer(r">>?", command):
        tail = command[m.end() :].lstrip()
        if tail.startswith(("/dev/null", "&1", "&2")):
            continue
        return True
    return False


def assert_readonly(command: str, shell: str) -> None:
    """注册期校验命令只读;违反时抛 ValueError(模块导入即失败,fail-fast)。

    shell: "sh"(Linux) 或 "ps"(Windows PowerShell)。
    """
    # 命令替换可藏任意写操作,一律禁止
    if "$(" in command or "`" in command:
        raise ValueError(f"只读校验失败(含命令替换): {command[:80]}")
    if _has_illegal_redirect(command):
        raise ValueError(f"只读校验失败(含文件重定向): {command[:80]}")

    patterns = _PS_WRITE_PATTERNS if shell == "ps" else _SHELL_WRITE_PATTERNS
    for pattern in patterns:
        if re.search(pattern, command, re.IGNORECASE):
            raise ValueError(f"只读校验失败(命中写入原语 {pattern!r}): {command[:80]}")


# ══════════════════════════════════════════════════════════════════════
# 注册表:key -> {label, command, timeout}
# ══════════════════════════════════════════════════════════════════════


def _sh(label: str, command: str, timeout: int = 15) -> dict:
    assert_readonly(command, "sh")
    return {"label": label, "command": command, "timeout": timeout}


def _ps(label: str, command: str, timeout: int = 20) -> dict:
    assert_readonly(command, "ps")
    return {"label": label, "command": command, "timeout": timeout}


LINUX_DIAGNOSTICS: dict[str, dict] = {
    "system_info": _sh(
        "系统信息",
        "uname -a; cat /etc/os-release 2>/dev/null | head -4",
    ),
    "cpu": _sh("CPU 使用概况", "top -bn1 | head -12"),
    "load_uptime": _sh("负载与运行时间", "uptime"),
    "memory": _sh("内存使用", "free -m"),
    "disk": _sh("磁盘分区使用", "df -h"),
    # 目录级占用:磁盘告警归因要回答「哪个目录在涨」,df 只有分区级。全盘 walk
    # 在大文件系统上可能超时,超时即证据不可用(注册表 hint 已向 LLM 说明)。
    "disk_usage_top": _sh(
        "目录磁盘占用 TOP20",
        "du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -20",
        30,
    ),
    "disk_io": _sh(
        "磁盘 IO 统计",
        "command -v iostat >/dev/null 2>&1 && iostat -dx 1 2 || cat /proc/diskstats",
        20,
    ),
    "top_processes_cpu": _sh("CPU 占用 TOP10 进程", "ps aux --sort=-%cpu | head -11"),
    "top_processes_mem": _sh("内存占用 TOP10 进程", "ps aux --sort=-%mem | head -11"),
    "failed_services": _sh(
        "失败的服务",
        "systemctl --failed 2>/dev/null || true",
    ),
    "listening_ports": _sh("监听端口", "ss -tlnp"),
    "network": _sh("网络配置与路由", "ip addr; ip route"),
    "established_top": _sh(
        "活跃连接 TOP10",
        "ss -tn state established | awk 'NR!=1{print $4}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -10",
    ),
    "kernel_errors": _sh(
        "内核错误日志",
        "dmesg -l err,crit,alert,emerg 2>/dev/null | tail -20 || journalctl -k -p err -n 20 --no-pager 2>/dev/null",
        20,
    ),
    "app_errors": _sh(
        "近 24h 错误日志",
        "journalctl -p err --since '24 hours ago' --no-pager 2>/dev/null | tail -30",
        20,
    ),
    "traefik_routes": _sh(
        "Traefik 路由与 Compose 上下文",
        "if command -v docker >/dev/null 2>&1; then echo '## docker'; docker version --format 'Server={{.Server.Version}}' 2>&1 || true; echo '## containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Status}}' 2>&1 || true; echo '## traefik labels'; docker ps -aq | xargs -r docker inspect --format '{{.Name}}|status={{.State.Status}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|ports={{json .Config.ExposedPorts}}|labels={{json .Config.Labels}}' 2>&1 | grep -i 'traefik\\.' || echo '未发现 Traefik 路由标签'; echo '## compose context'; docker ps -aq | xargs -r docker inspect --format '{{.Name}}|project={{index .Config.Labels \"com.docker.compose.project\"}}|service={{index .Config.Labels \"com.docker.compose.service\"}}|workdir={{index .Config.Labels \"com.docker.compose.project.working_dir\"}}|files={{index .Config.Labels \"com.docker.compose.project.config_files\"}}' 2>&1 | grep -Ei 'traefik|project=' || true; if command -v curl >/dev/null 2>&1; then echo '## api routers'; for port in 8080 8081; do echo \"port=$port\"; curl -fsS --max-time 3 \"http://127.0.0.1:$port/api/http/routers\" 2>/dev/null | head -c 5000 || true; echo; done; fi; else echo '未安装 Docker'; fi",
        25,
    ),
    "traefik_runtime": _sh(
        "Traefik 运行状态与已加载路由",
        "if command -v docker >/dev/null 2>&1; then echo '## traefik containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' | grep -i traefik || echo '未发现 Traefik 容器'; echo '## runtime details'; docker ps -aq --filter name=traefik | head -1 | xargs -r docker inspect --format '{{.Name}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}},{{end}}|cmd={{json .Config.Cmd}}|entrypoint={{json .Config.Entrypoint}}|mounts={{range .Mounts}}{{.Source}}:{{.Destination}},{{end}}'; echo '## published dashboard api'; for port in 8080 8081; do echo \"port=$port\"; curl -fsS --max-time 3 \"http://127.0.0.1:$port/api/http/routers\" 2>/dev/null | head -c 5000 || true; echo; done; else echo '未安装 Docker'; fi",
        20,
    ),
    "docker_runtime": _sh(
        "Docker 容器与 Compose 运行状态",
        "if command -v docker >/dev/null 2>&1; then echo '## containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>&1; echo '## compose and runtime'; docker ps -aq | xargs -r docker inspect --format '{{.Name}}|state={{.State.Status}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|project={{index .Config.Labels \"com.docker.compose.project\"}}|service={{index .Config.Labels \"com.docker.compose.service\"}}|networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}},{{end}}|mounts={{range .Mounts}}{{.Destination}},{{end}}' 2>&1; else echo '未安装 Docker'; fi",
        20,
    ),
    # 容器内进程级取证:PID 1 命令行(Entrypoint+Cmd) + 运行中容器的进程快照。
    # 全部只读,用于回答“容器里到底在跑什么”,避免把镜像名/端口推断写成结论。
    "docker_processes": _sh(
        "容器内进程与 PID 1 命令行",
        "if command -v docker >/dev/null 2>&1; then echo '## pid1'; docker ps -aq | xargs -r docker inspect --format '{{.Name}}|state={{.State.Status}}|pid1={{json .Config.Entrypoint}} {{json .Config.Cmd}}|user={{.Config.User}}|image={{.Config.Image}}' 2>&1; echo '## processes'; docker ps -q | xargs -r -I{} docker exec {} ps -eo pid,ppid,user,pcpu,pmem,comm,args 2>&1; else echo '未安装 Docker'; fi",
        40,
    ),
    "docker_logs": _sh(
        "容器近期错误日志",
        "if command -v docker >/dev/null 2>&1; then docker ps -a --format '{{.Names}}|{{.State.Status}}' 2>&1 | while IFS='|' read -r name state; do echo \"## $name [$state]\"; docker logs --tail 120 \"$name\" 2>&1 | grep -Ei 'error|exception|failed|fatal|panic|warn|critical|timeout|refused|denied' | tail -40; done; else echo '未安装 Docker'; fi",
        40,
    ),
    "docker_stats": _sh(
        "容器资源占用快照",
        "if command -v docker >/dev/null 2>&1; then docker stats --no-stream --format '{{.Name}}|cpu={{.CPUPerc}}|mem={{.MemUsage}} ({{.MemPerc}})|net={{.NetIO}}|pids={{.PIDs}}' 2>&1; else echo '未安装 Docker'; fi",
        25,
    ),
    "running_services": _sh(
        "运行中的 systemd 服务",
        "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -30",
    ),
    "zombie_processes": _sh(
        "僵尸进程",
        "ps -eo stat,ppid,pid,comm | grep -E '^Z' || echo 'no zombie'",
    ),
    "recent_logins": _sh("最近登录记录", "last -n 15"),
}

WINDOWS_DIAGNOSTICS: dict[str, dict] = {
    # systeminfo 实测要 10~30s(慢在收集全部热修丁/网卡/共享),CIM 查询 1~2s,
    # 诊断一步就快一个量级;字段对归因足够(发行版/版本/架构/最近启动)。
    "system_info": _ps(
        "系统信息",
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,LastBootUpTime,InstallDate | Format-Table -AutoSize",
        15,
    ),
    "memory": _ps(
        "内存使用",
        "$os=Get-CimInstance Win32_OperatingSystem; Write-Output ('TotalMB={0} FreeMB={1} UsagePct={2:N1}' -f ($os.TotalVisibleMemorySize/1KB), ($os.FreePhysicalMemory/1KB), (($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/$os.TotalVisibleMemorySize*100))",
    ),
    "disk": _ps(
        "磁盘分区使用",
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Format-Table DeviceID,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}},@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,1)}},@{N='UsagePct';E={[math]::Round(($_.Size-$_.FreeSpace)/$_.Size*100,1)}} -AutoSize",
    ),
    "disk_io": _ps(
        "磁盘 IO 速率",
        "Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk | Where-Object {$_.Name -ne '_Total'} | Format-Table Name,DiskReadBytesPersec,DiskWriteBytesPersec,PercentDiskTime -AutoSize",
    ),
    "top_processes_cpu": _ps(
        "CPU 占用 TOP10 进程",
        "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,Id,CPU | Format-Table -AutoSize",
    ),
    "top_processes_mem": _ps(
        "内存占用 TOP10 进程",
        "Get-Process | Sort-Object WS -Descending | Select-Object -First 10 Name,Id,@{N='MB';E={[math]::Round($_.WS/1MB,1)}} | Format-Table -AutoSize",
    ),
    # 与健康巡检共用同一条命令（单一真源）。旧版枚举「StartType=Automatic 且
    # 没在运行」的服务，标签还写着「应运行但未运行」——等于向 LLM 断言这些
    # 都是故障。RemoteRegistry、spice-agent、sppsvc(延迟启动,干完活就退出)
    # 这类本来就该停着的服务会被当成诊断结论写进报告，比巡检误报 critical
    # 更糟——LLM 还会围绕它编一段原因分析。
    "failed_services": _ps(
        f"服务异常(SCM 失败事件,近 {WINDOWS_SERVICE_FAILURE_WINDOW_HOURS} 小时)",
        WINDOWS_SCM_FAILURE_COMMAND,
        25,
    ),
    "listening_ports": _ps("监听端口", "netstat -ano | findstr LISTENING"),
    "network": _ps("网络配置", "ipconfig /all", 15),
    "event_errors_system": _ps(
        "系统错误事件(近 15 条)",
        "Get-EventLog -LogName System -EntryType Error -Newest 15 | Format-Table TimeGenerated,Source,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(80,$_.Message.Length))}} -Wrap",
    ),
    "event_errors_app": _ps(
        "应用错误事件(近 15 条)",
        "Get-EventLog -LogName Application -EntryType Error -Newest 15 | Format-Table TimeGenerated,Source,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(80,$_.Message.Length))}} -Wrap",
    ),
    "traefik_routes": _ps(
        "Traefik 路由与 Compose 上下文",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { Write-Output '## docker'; docker version --format 'Server={{.Server.Version}}' 2>&1; Write-Output '## containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Status}}' 2>&1; Write-Output '## traefik labels'; $ids=docker ps -aq; if ($ids) { docker inspect $ids --format '{{.Name}}|{{range $k,$v := .Config.Labels}}{{$k}}={{$v}};{{end}}' 2>&1 | Select-String -Pattern 'traefik' -CaseSensitive:$false } else { Write-Output '未发现容器' }; Write-Output '## compose context'; if ($ids) { docker inspect $ids --format '{{.Name}}|project={{index .Config.Labels \"com.docker.compose.project\"}}|service={{index .Config.Labels \"com.docker.compose.service\"}}|workdir={{index .Config.Labels \"com.docker.compose.project.working_dir\"}}|files={{index .Config.Labels \"com.docker.compose.project.config_files\"}}' 2>&1 | Select-String -Pattern 'traefik|project=' -CaseSensitive:$false }; Write-Output '## api routers'; foreach ($port in 8080,8081) { Write-Output \"port=$port\"; try { $json=(Invoke-RestMethod -Uri \"http://127.0.0.1:$port/api/http/routers\" -TimeoutSec 3 | ConvertTo-Json -Depth 8 -Compress); $json.Substring(0,[Math]::Min(5000,$json.Length)) } catch { } } } else { Write-Output '未安装 Docker' }",
        25,
    ),
    "traefik_runtime": _ps(
        "Traefik 运行状态与已加载路由",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { Write-Output '## traefik containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>&1 | Select-String -Pattern 'traefik' -CaseSensitive:$false; Write-Output '## runtime details'; $id=docker ps -aq --filter name=traefik | Select-Object -First 1; if ($id) { docker inspect $id --format '{{.Name}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}},{{end}}|cmd={{json .Config.Cmd}}|entrypoint={{json .Config.Entrypoint}}|mounts={{range .Mounts}}{{.Source}}:{{.Destination}},{{end}}' }; Write-Output '## published dashboard api'; foreach ($port in 8080,8081) { Write-Output \"port=$port\"; try { $json=(Invoke-RestMethod -Uri \"http://127.0.0.1:$port/api/http/routers\" -TimeoutSec 3 | ConvertTo-Json -Depth 8 -Compress); $json.Substring(0,[Math]::Min(5000,$json.Length)) } catch { } } } else { Write-Output '未安装 Docker' }",
        20,
    ),
    "docker_runtime": _ps(
        "Docker 容器与 Compose 运行状态",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { Write-Output '## containers'; docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>&1; Write-Output '## compose and runtime'; $ids=docker ps -aq; if ($ids) { docker inspect $ids --format '{{.Name}}|state={{.State.Status}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|project={{index .Config.Labels \"com.docker.compose.project\"}}|service={{index .Config.Labels \"com.docker.compose.service\"}}|networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}},{{end}}|mounts={{range .Mounts}}{{.Destination}},{{end}}' 2>&1 } } else { Write-Output '未安装 Docker' }",
        20,
    ),
    "docker_processes": _ps(
        "容器内进程与 PID 1 命令行",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { Write-Output '## pid1'; $ids=docker ps -aq; if ($ids) { docker inspect $ids --format '{{.Name}}|state={{.State.Status}}|pid1={{json .Config.Entrypoint}} {{json .Config.Cmd}}|user={{.Config.User}}|image={{.Config.Image}}' 2>&1; Write-Output '## processes'; $rids=docker ps -q; if ($rids) { foreach ($id in $rids) { docker exec $id ps -eo pid,ppid,user,pcpu,pmem,comm,args 2>&1 } } else { Write-Output '无运行中容器' } } else { Write-Output '未发现容器' } } else { Write-Output '未安装 Docker' }",
        40,
    ),
    "docker_logs": _ps(
        "容器近期错误日志",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { $ids=docker ps -aq; if ($ids) { foreach ($id in $ids) { $n=docker inspect --format '{{.Name}} [{{.State.Status}}]' $id; Write-Output \"## $n\"; $lg=docker logs --tail 120 $id 2>&1; if ($lg) { $lg | Select-String -Pattern 'error|exception|failed|fatal|panic|warn|critical|timeout|refused|denied' -CaseSensitive:$false | Select-Object -Last 40 } } } else { Write-Output '未发现容器' } } else { Write-Output '未安装 Docker' }",
        40,
    ),
    "docker_stats": _ps(
        "容器资源占用快照",
        "if (Get-Command docker -ErrorAction SilentlyContinue) { docker stats --no-stream --format '{{.Name}}|cpu={{.CPUPerc}}|mem={{.MemUsage}} ({{.MemPerc}})|net={{.NetIO}}|pids={{.PIDs}}' 2>&1 } else { Write-Output '未安装 Docker' }",
        25,
    ),
    "recent_logins": _ps(
        "最近登录事件",
        "Get-EventLog -LogName Security -InstanceID 4624 -Newest 10 | Format-Table TimeGenerated,@{N='Msg';E={$_.Message.Substring(0,[math]::Min(90,$_.Message.Length))}} -Wrap",
    ),
    "installed_updates": _ps(
        "最近安装的补丁",
        "Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 10 HotFixID,Description,InstalledOn | Format-Table -AutoSize",
    ),
    "uptime": _ps(
        "运行时间",
        "$bt=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; (Get-Date)-$bt | Select-Object Days,Hours,Minutes",
        15,
    ),
}


def get_diagnostics_for_device(device: Device) -> dict[str, dict]:
    """按设备类型返回可用诊断项。网络设备不支持(Agent 面只覆盖服务器)。"""
    if device.is_windows:
        return WINDOWS_DIAGNOSTICS
    return LINUX_DIAGNOSTICS


def get_diagnostic(device: Device, key: str) -> dict | None:
    """取单个诊断项;key 不在注册表(含 LLM 幻觉出的 key)返回 None——拒绝执行。"""
    return get_diagnostics_for_device(device).get(key)
