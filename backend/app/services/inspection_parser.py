"""
Inspection result parser — parse CLI output per item type and evaluate thresholds.

Each parser returns: { success, value, unit, status, details }
"""

import logging
import re

from app.services.inspection_commands import (
    SCM_FAILURE_EVENT_IDS,
    WINDOWS_SERVICE_FAILURE_WINDOW_HOURS,
)

logger = logging.getLogger(__name__)

# ── Thresholds ──

THRESHOLDS: dict[str, dict[str, float]] = {
    "cpu": {"warning": 70, "critical": 85},
    "memory": {"warning": 75, "critical": 90},
    "disk": {"warning": 80, "critical": 95},
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
        # top -bn1 → "%Cpu(s):  5.2 us,  1.3 sy,  0.0 ni, 92.9 id, ..."
        # 中文 locale 下冒号是全角「：」。只取 us 会把内核态(sy)与 IO 等待(wa)
        # 饱和误判成空闲;取 idle 的补集。
        m = re.search(r"%Cpu\(s\)[:：].*?([\d.]+)\s+id\b", raw)
        if m:
            usage = round(max(0.0, min(100.0, 100.0 - float(m.group(1)))), 1)
        else:
            m = re.search(r"%Cpu\(s\)[:：]\s+([\d.]+)\s+us", raw)
            if m:
                usage = float(m.group(1))

    elif target_type == "windows":
        usage = _last_percent(raw)

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
        # free -m → "Mem:  16384  12288   4096  ..."；中文 locale 下行标签是
        # 「内存：」（表头行仍是英文，procps 只翻译行标签）。不能只认 "Mem:"
        # 前缀——按「首个带 ≥4 个数值列的数据行」定位内存行（表头非数字、
        # Swap 行只有 3 列，天然排除），LC_ALL=C 与本地化输出都能解析。
        for line in raw.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                nums = [float(p) for p in parts[1:]]
            except ValueError:
                continue
            if len(nums) >= 4:
                total = nums[0]
                used = nums[1]
                usage = (used / total * 100) if total > 0 else 0
                details = {"total_mb": int(total), "used_mb": int(used)}
                break

    elif target_type == "windows":
        # Percentage output like "75.3" — take the last plausible percentage.
        usage = _last_percent(raw)

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


def _parse_load(raw: str, target_type: str) -> dict:
    """Parse system load average (Linux: uptime)."""
    # "10:30:00 up 5 days, 3:20, 2 users, load average: 0.15, 0.10, 0.08"
    # 中文 locale 下是「平均负载：」（全角冒号），两种都认。
    m = re.search(
        r"(?:load average|平均负载)[:：]\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", raw
    )
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


# sshd 握手噪声：描述的是*远端客户端*的行为（连上就挂、发垃圾数据），不是本机
# 故障。最典型的来源是平台自己的监控探针——_check_tcp_port 每 MONITOR_INTERVAL
# 做一次裸 TCP connect 再 close，在被探主机的 sshd 日志里恰好留下一条
# kex_exchange_identification。不过滤的话，任何被监控的 Linux 主机「异常日志」
# 都会被自家探针顶成常年 warning（系统性假阳性）。
# 只从计数与状态里剔除；原始输出原样保留，安全线索（如扫描 22 端口）不丢。
_SSHD_HANDSHAKE_NOISE = (
    "kex_exchange_identification",
    "banner line contains invalid characters",
)


# pvedaemon 的 QGA 探测噪声：'guest-ping' 超时是「guest 里没装/没跑 agent」
# 这个慢性配置状态（PVE 把它记成 err 级），不是宿主机故障。平台自己的
# guest IP 刷新循环（agent 失败退避 30min 重试）会让它每半小时稳定产生
# 一条，24h 最多 48 条 → 对 PVE 宿主机巡检时「异常日志」常年 warning，
# 真实故障被海淹没。与 sshd 握手噪声同类：只从计数与状态里剔除，
# 原始输出保留（装 agent 后日志自然消失，不影响发现真实配置问题）。
_PVE_AGENT_PROBE_NOISE = ("qmp command 'guest-ping' failed",)


def _is_sshd_handshake_noise(line: str) -> bool:
    return any(token in line for token in _SSHD_HANDSHAKE_NOISE)


def _is_pve_agent_probe_noise(line: str) -> bool:
    return any(token in line for token in _PVE_AGENT_PROBE_NOISE)


def _parse_logs(raw: str, target_type: str) -> dict:
    """Parse error logs (journalctl).

    sshd 握手噪声与 pvedaemon guest-ping 超时不计入条数与状态（前者是
    远端客户端行为，含平台自身监控探针；后者是“没装 agent”的慢性配置
    状态且由平台 QGA 轮询自己触发）；原始输出仍完整保留在
    details.output，安全线索不丢。被滤掉的条数记在 details.filtered_noise，
    便于排查「为什么计数比原始少」。
    """
    all_lines = [l for l in raw.strip().splitlines() if l.strip()]
    lines = [
        l
        for l in all_lines
        if not (_is_sshd_handshake_noise(l) or _is_pve_agent_probe_noise(l))
    ]
    noise = len(all_lines) - len(lines)
    return {
        "success": True,
        "value": str(len(lines)),
        "unit": "条",
        "status": "warning" if len(lines) > 10 else "normal",
        "details": {
            "error_count": len(lines),
            "filtered_noise": noise,
            "output": raw[:500],
        },
    }


# systemd 单元名后缀。`systemctl --failed` 的表格除单元行外还有表头、
# LOAD/ACTIVE/SUB 图例和两行提示文案，只有首字段是单元名的行才算失败服务。
_UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".device",
    ".mount",
    ".automount",
    ".swap",
    ".target",
    ".path",
    ".timer",
    ".slice",
    ".scope",
)


def _failed_unit_lines(raw: str) -> list[str]:
    """从 `systemctl --failed` 输出里挑出真正的失败单元行。

    失败单元行以 "●" 起头，图例与提示行的首字段不是单元名。旧实现按前缀把 "●"
    行整条丢掉、又把 "LOAD   = ..." 这类图例当成服务，于是真有服务挂掉时
    既漏掉真凶、又混进说明文字（单机 1 个失败服务会被数成 4 个）。
    """
    units: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip().removeprefix("●").strip()
        if line and line.split()[0].endswith(_UNIT_SUFFIXES):
            units.append(line)
    return units


def _windows_scm_failures(raw: str) -> list[dict]:
    """解析 Windows 服务异常命令的输出。

    输出契约(见 inspection_commands.WINDOWS_SCM_FAILURE_COMMAND)：每行
    ``发生次数|事件ID|服务名``，已按「事件ID|服务名」聚合过。

    服务名取自事件的 ReplacementStrings 而非 Message，因此与系统语言无关
    （中文 Windows 上 Message 是中文，用正则抽服务名会直接失效）。
    """
    events: list[dict] = []
    for raw_line in raw.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 服务名里理论上可以含 '|'，所以只切前两个分隔符
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        try:
            occurrences = int(parts[0].strip())
            event_id = int(parts[1].strip())
        except ValueError:
            # 不是我们要求的形状(例如旧版本的 Format-Table 残留输出)，跳过
            continue
        service = parts[2].strip() if len(parts) > 2 else ""
        events.append(
            {
                "service": service,
                "event_id": event_id,
                "event_meaning": SCM_FAILURE_EVENT_IDS.get(event_id, ""),
                "occurrences": max(occurrences, 1),
            }
        )
    return events


def _parse_failed_services(raw: str, target_type: str) -> dict:
    """统计「真的出了问题」的服务。

    Linux 走 ``systemctl --failed``；Windows 查系统日志里 Service Control Manager
    的失败事件(7000/7001/7009/7011/7023/7024/7031/7034)，按**服务名去重**后计数。

    为什么按服务名而不是按事件数：同一个服务同时触发 7031 与 7034、或在 24 小时内
    反复崩溃，都只是「一个服务有问题」；按事件数计会把崩溃循环放大成 critical。
    发生次数保留在 details 与原始输出里，严重程度不丢。
    """
    if target_type == "windows":
        events = _windows_scm_failures(raw)
        by_service: dict[str, dict] = {}
        for event in events:
            key = event["service"] or f"未知服务(事件 {event['event_id']})"
            slot = by_service.setdefault(
                key, {"event_ids": set(), "occurrences": 0, "meanings": []}
            )
            slot["event_ids"].add(event["event_id"])
            slot["occurrences"] += event["occurrences"]
            if (
                event["event_meaning"]
                and event["event_meaning"] not in slot["meanings"]
            ):
                slot["meanings"].append(event["event_meaning"])
        count = len(by_service)
        # 崩溃次数多的排前面，方便先看到最吵的那个
        ordered = sorted(
            by_service.items(), key=lambda kv: (-kv[1]["occurrences"], kv[0])
        )
        services = [
            f"{name}｜{'、'.join(slot['meanings']) or '事件 ' + ','.join(str(i) for i in sorted(slot['event_ids']))}"
            f"｜{slot['occurrences']} 次"
            for name, slot in ordered
        ]
        details = {
            "failed_count": count,
            "services": services[:10],
            "window_hours": WINDOWS_SERVICE_FAILURE_WINDOW_HOURS,
            "event_count": sum(event["occurrences"] for event in events),
        }
    else:
        svc_lines = _failed_unit_lines(raw)
        count = len(svc_lines)
        details = {
            "failed_count": count,
            "services": [line.strip()[:80] for line in svc_lines[:10]],
        }

    # 走 THRESHOLDS：0 个 normal，1~2 个 warning，3 个及以上 critical。
    # Windows 侧现在数的是真实失败而非「停着的自动服务」，所以 1 个就值得告警。
    status = _evaluate_threshold("failed_services", count)
    return {
        "success": True,
        "value": str(count),
        "unit": "个",
        "status": status,
        "details": details,
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


# Windows 事件日志已知噪声（来源, 事件ID）。这些都是微软官方定性为可安全忽略、
# 或属于系统自恢复行为的事件，计入条数只会把「事件日志」项刷成警告：
#   DCOM 10010        服务器未在超时内注册（多为系统更新/服务启动慢，且消息文件常缺失）
#   DCOM 10016        分布式 COM 本地激活权限，微软文档明确“by design，可忽略”
#   SCM 7030          “标记为交互服务但系统不允许交互”提示（系统更新后常见，服务照常工作）
#   TPM-WMI 1801      “需要更新安全启动 CA/密钥”——SeaBIOS 等虚拟机固件没有可更新的
#                     Secure Boot DB，虚拟 TPM 上周期性提示，虚拟化平台通病
#   Schannel 36874    远端客户端发起低版本/无共同密码套件的 TLS 握手被服务器拒绝，
#                     属远端行为（扫描器/老旧客户端/监控探针），与 Linux sshd 握手
#                     噪声同性质；且爆发时一秒几十条会占满采样窗口淹没真实事件
#   SCM 7040/7036 类状态切换是 Information 级，进不了 Error 查询，不在此列
_WINDOWS_EVENT_NOISE_IDS: frozenset[tuple[str, int]] = frozenset(
    {
        ("DCOM", 10010),
        ("DCOM", 10016),
        ("Service Control Manager", 7030),
        ("Microsoft-Windows-TPM-WMI", 1801),
        ("Schannel", 36874),
    }
)

# SCM 7023「服务因下列错误而停止」中属于自恢复的组合：错误码 21(ERROR_NOT_READY)
# 与 2147942414(ERROR_FILE_NOT_FOUND 的 HRESULT 形式) 落在打印/网络辅助服务上，
# 多为无机可用时退出或重启即恢复，不是持续故障。
# 注意消息里是服务**显示名**（"IP Helper" 这种带空格的），不是服务名（iphlpsvc），
# 所以用包含匹配而不是精确等值；中英文系统的显示名都是英文原名。
_WINDOWS_EVENT_NOISE_7023_NAMES = (
    "spooler",
    "printnotify",
    "print workflow",
    "ip helper",
    "iphlpsvc",
)
_WINDOWS_EVENT_NOISE_7023_CODES = ("%%21", "%%2147942414")

# 事件日志项最多展示多少类有效事件
_EVENT_LOGS_MAX_KEPT = 20

# 一天内出现多少「类」不同的错误才报警。聚合后同一事件爆发多少次都算 1 类，
# 超过 5 类不同错误基本是系统真出问题了。
_EVENT_LOGS_WARN_CLASSES = 5


def _is_windows_event_noise(source: str, event_id: int, message: str) -> bool:
    """判断一条 Windows Error 级系统事件是否为已知慢性噪声（每天复发型）。"""
    if (source, event_id) in _WINDOWS_EVENT_NOISE_IDS:
        return True
    if source == "Service Control Manager" and event_id == 7023:
        lowered = message.lower()
        if any(n in lowered for n in _WINDOWS_EVENT_NOISE_7023_NAMES) and any(
            c in message for c in _WINDOWS_EVENT_NOISE_7023_CODES
        ):
            return True
    return False


def _parse_event_logs(raw: str, target_type: str) -> dict:
    """解析 Windows 系统日志 Error 级事件（管道格式，见 inspection_commands.event_logs）。

    系统性防误报口径：
      1. 命令侧已限定最近 24 小时；
      2. 按「来源+事件ID」聚类，爆发 N 次只算 1 类（展示为 ``N× 样本行``），
         按「类数 > 5」判 warning——未知新型噪音天然只有 1 类，不会误报；
      3. 已知慢性噪声（每天复发型）按噪声表剔除，被滤条数记 details.filtered_noise；
         原始输出完整保留在 raw_output，安全线索不丢。
    无法按新格式解析的行（旧 Format-Table 残留等）每行单独成类，宁可多报不漏报。
    """
    noise = 0
    # key: (来源, 事件ID)；value: [样本行, 次数]。无法解析的行用整行做 key。
    groups: dict[tuple[str, str], list] = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) >= 4:
            try:
                event_id = int(parts[2].strip())
            except ValueError:
                groups.setdefault((line, ""), [line, 0])[1] += 1
                continue
            source = parts[1].strip()
            if _is_windows_event_noise(source, event_id, parts[3]):
                noise += 1
                continue
            key = (source, parts[2].strip())
            groups.setdefault(key, [line, 0])[1] += 1
        else:
            groups.setdefault((line, ""), [line, 0])[1] += 1
    # 按发生次数降序展示，同类取最新一条作样本（命令输出本身按时间倒序）
    classes = sorted(groups.values(), key=lambda g: -g[1])[:_EVENT_LOGS_MAX_KEPT]
    shown = [f"{count}× {sample}" if count > 1 else sample for sample, count in classes]
    return {
        "success": True,
        "value": str(len(classes)),
        "unit": "类",
        "status": "warning" if len(classes) > _EVENT_LOGS_WARN_CLASSES else "normal",
        "details": {
            "error_count": len(classes),
            "filtered_noise": noise,
            "output": "\n".join(shown)[:500],
        },
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
    r"""Parse Windows uptime.

    兼容两种输出形状：

    * ``Select-Object Days,Hours,Minutes`` 的表格（当前命令的真实输出）：
      表头一行、分隔线一行、数字一行，列顺序按表头取；
    * ``3 Days, 12 Hours, 45 Minutes`` 这类自然语言串。

    旧版只认后者，而正则 ``(\d+)\s*Days?`` 要求「数字在前」，对表格输出
    永远匹配不上，于是无论机器跑了多久都返回 ``0天0时0分`` 并且报 normal。
    现在两者都认；都认不出来时如实返回失败，不再假装成功。
    """
    days = hours = minutes = None

    # 形状 A：自然语言（数字在前）
    for key, pattern in (
        ("days", r"(\d+)\s*Days?\b"),
        ("hours", r"(\d+)\s*Hours?\b"),
        ("minutes", r"(\d+)\s*Minutes?\b"),
    ):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            value = int(m.group(1))
            if key == "days":
                days = value
            elif key == "hours":
                hours = value
            else:
                minutes = value

    # 形状 B：Select-Object 表格——按表头列序定位数字行
    if days is None and hours is None and minutes is None:
        lines = raw.strip().splitlines()
        for idx, line in enumerate(lines):
            header = re.findall(r"Days|Hours|Minutes", line, re.IGNORECASE)
            if len(header) < 2:
                continue
            # 表头之后的第一行纯数字就是值；分隔线 "---- -----" 会被跳过
            for value_line in lines[idx + 1 :]:
                tokens = value_line.split()
                if len(tokens) != len(header):
                    continue
                if not all(re.fullmatch(r"\d+", t) for t in tokens):
                    continue
                # 上面已校验 len(tokens) == len(header)，strict 用于把这个前提显式化
                for column, token in zip(header, tokens, strict=True):
                    if column.lower() == "days":
                        days = int(token)
                    elif column.lower() == "hours":
                        hours = int(token)
                    else:
                        minutes = int(token)
                break
            if days is not None or hours is not None or minutes is not None:
                break

    if days is None and hours is None and minutes is None:
        return {
            "success": False,
            "value": None,
            "unit": None,
            "status": "error",
            "details": {"raw": raw[:200]},
        }

    days = days or 0
    hours = hours or 0
    minutes = minutes or 0
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
