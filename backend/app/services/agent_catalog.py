"""诊断项目录:关键词收窄、Docker 门控、逐项提示(从 agent.py 拆出,Phase-1)。

红线:关键词收窄只决定「优先取哪些」,不是可执行边界——全量注册表
允许按需调用(见 _full_catalog 与 _dispatch 的回退逻辑)。
"""

from __future__ import annotations

from app.models.device import Device
from app.services import agent_commands, agent_primitives

_SUPPLEMENT_BUDGET = 5

# 依赖 Docker 运行时的诊断项(命令内部都以 `command -v docker` 守卫)。
# docker_runtime/traefik_routes 是「探测项」本身,不参与门控——它们跑一次
# 就能确认 Docker 是否存在;其余项在其确认不存在后直接短路。
_DOCKER_GATED_KEYS = frozenset(
    {
        "docker_processes",
        "docker_logs",
        "docker_stats",
        "docker_logs_container",
        "docker_inspect_container",
        "docker_exec_read",
        "docker_images",
        "docker_network_ls",
        "traefik_runtime",
    }
)


def _docker_absent_note(steps: list[dict]) -> str:
    """从已执行步骤里找 Docker 探测结论:返回未安装说明,无则空串。

    三个探测项(docker_runtime / traefik_routes / traefik_runtime 命令
    兜底)输出「未安装 Docker」即认定缺席;正常输出容器清单则视为存在。
    """
    for step in steps:
        if step.get("tool") != "run_diagnostic":
            continue
        if str(step.get("key") or "") in (
            "docker_runtime",
            "traefik_routes",
            "traefik_runtime",
        ):
            preview = str(step.get("preview") or "")
            if "未安装 Docker" in preview:
                return "未安装 Docker"
    return ""


_DIAGNOSTIC_HINTS = {
    "system_info": "操作系统、内核和基础环境信息;不能证明业务服务健康。",
    "cpu": "一次性 CPU 概况和进程快照;不能代表历史趋势。",
    "load_uptime": "当前负载与运行时长;需结合 CPU/进程判断原因。",
    "memory": "当前内存和交换分区快照。",
    "disk": "文件系统分区容量使用;不包含应用级目录增长原因。",
    "disk_usage_top": "目录级磁盘占用 TOP20;回答「哪个目录在涨」。大文件系统上全盘 walk 可能超时,超时或被截断时如实说明证据不可用,不要用分区级数据冒充目录级结论。",
    "disk_io": "磁盘 IO 当前采样;设备不支持 iostat 时可能是累计计数器。",
    "top_processes_cpu": "当前 CPU 占用最高进程快照。",
    "top_processes_mem": "当前内存占用最高进程快照。",
    "process_list": "全量进程清单(名称↔PID);把 listening_ports/netstat 里的 PID 对上具体进程用这一项。不含进程命令行参数。",
    "messages_error_count": "文件式系统日志(/var/log/messages 或 /var/log/syslog)的错误计数与近 15 条样本;RHEL/CentOS 或 journalctl 未持久化的机器用这项。",
    "failed_services": "系统服务真实失败的证据(Linux: systemctl --failed;Windows: SCM 失败事件,近 24h);输出为空即无服务故障,不要自行推断“服务未运行”;不等同于指定业务接口可用。",
    "listening_ports": "本机监听端口及进程信息;不能证明外部网络可达。",
    "network": "本机网卡和路由配置;不能替代端到端连通性测试。",
    "established_top": "当前已建立连接的来源统计快照。",
    "kernel_errors": "内核近期错误日志;时间范围和权限可能影响结果。",
    "app_errors": "近 24 小时系统日志中的错误;不包含所有应用日志文件。",
    "zombie_processes": "僵尸进程快照。",
    "recent_logins": "最近登录记录;仅反映系统审计日志可见内容。",
    "traefik_routes": "所有 Docker 容器的 Traefik labels、Compose 归属和容器上下文;用于判断声明式路由。",
    "traefik_runtime": "Traefik 容器状态、网络、挂载、启动参数及已发布 Dashboard API 路由(若可访问)。",
    "docker_runtime": "所有 Docker 容器状态、健康状态、Compose service、网络和挂载。",
    "docker_processes": "每个容器的 PID 1 命令行(Entrypoint/Cmd/运行用户)与运行中容器的进程快照;容器内实际运行内容的直接证据。",
    "docker_logs": "每个容器近期日志中的错误/异常行;判断容器为何退出、重启或异常的取证项。",
    "docker_inspect_container": "指定容器的完整 inspect 详情(状态/重启次数/挂载/网络/启动命令/环境变量);输出统一脱敏与截断。",
    "docker_images": "本机 Docker 镜像清单(回答「机器上部署了什么」)。",
    "docker_network_ls": "Docker 网络清单(容器间网络归属)。",
    "docker_exec_read": "读容器内指定文件(docker exec cat,路径限绝对路径且过敏感拒绝清单);回答「这个容器具体配置了什么」。",
    "process_detail": "指定 PID 的详情:完整命令行、工作目录(cwd)、父进程——把「某个 python/sh 进程」归属到具体业务/目录。",
    "list_dir": "列目录内容(ls -la,路径过敏感拒绝清单);回答「这个目录里有什么/配置文件在哪」。",
    "docker_stats": "各容器的 CPU/内存/网络 IO 快照;回答“哪个容器在占资源”。",
    "running_services": "当前运行中的 systemd 服务清单;与 failed_services 互补。",
    "docker_logs_container": (
        "指定容器近 N 行完整日志;参数: name=容器名, tail=行数(20~500,默认 200)。"
        "排查单个容器为什么退出/重启用它,比 docker_logs 的全容器错误行更完整。"
    ),
    "service_status": (
        "指定 systemd 服务的状态与最近日志行;参数: unit=服务名(如 nginx.service)。"
        "注意 exit_code 非 0 往往只代表服务非 running,输出本身是证据。"
    ),
    "service_journal": (
        "指定 systemd 服务的近期 journal 日志;参数: unit=服务名, lines=行数(默认 200)。"
    ),
    "read_file": (
        "读取指定文件尾部(应用配置/日志文件);参数: path=绝对路径, lines=行数(默认 200)。"
        "敏感路径(私钥/shadow/.env/凭据类)会被拒绝;输出中的口令类字段会被脱敏。"
    ),
    "du_path": (
        "指定路径的目录级磁盘占用 TOP20;参数: path=绝对路径, depth=1~3(默认 1)。"
        "回答「这个目录多大/哪一层在涨」。"
    ),
    "ps_filter": (
        "按名称片段过滤进程;参数: pattern=进程名片段(不含空格)。"
        "TOP10 快照之外的进程用它查;输出为空即没有匹配的进程。"
    ),
    "tcp_probe": (
        "从目标机向指定地址发起 TCP 连通探测;参数: host=主机名或 IPv4,"
        " port=端口(1~65535)。回答「从这台机器连不连得上」;连通≠服务健康,"
        "不支持 IPv6 字面量地址。"
    ),
    "http_probe": (
        "探测本机(127.0.0.1)HTTP 服务的状态码与耗时;参数: port=端口,"
        " path=URL 路径(默认 /)。回答「本机 web 服务返回什么」——TCP 通但"
        "HTTP 5xx/超时时用它;仅限回环,不能探测外部地址。"
    ),
    "win_event_filter": (
        "Windows 事件日志按来源/消息子串过滤(近 300 条取前 25);参数:"
        " log=System 或 Application, pattern=子串。比固定项"
        " event_errors_system/app 更精准地查指定服务/来源的事件。"
    ),
    "find_name": (
        "指定目录下按文件名片段找文件(按大小倒序 TOP20,深度≤4);参数:"
        " path=绝对路径, name=文件名片段。回答「那个大文件在哪/配置文件在哪」。"
    ),
    "win_service_status": (
        "指定 Windows 服务的状态与配置(运行状态/启动类型/运行账户/进程 ID/"
        "退出码);参数: name=服务名。"
    ),
    "memory_commit": (
        "Windows 内存提交/缓存口径:Commit 与 CommitLimit、系统缓存、内核池。"
        "机内任务管理器看不到高占用而宿主/面板显示高时,用它对照「分配口径 vs 实际使用」。"
    ),
    "event_errors_system": "Windows 系统错误事件。",
    "event_errors_app": "Windows 应用错误事件。",
    "uptime": "Windows 运行时长。",
    "installed_updates": "Windows 最近安装补丁。",
}


def _full_catalog(device: Device) -> dict[str, dict]:
    """完整目录 = 固定注册表 + 参数化原语(同一条只读红线,后者支持受控参数)。"""
    merged = dict(agent_commands.get_diagnostics_for_device(device))
    merged.update(agent_primitives.catalog_for(device))
    return merged


def _select_catalog(
    device: Device,
    question: str,
    focus: list[str] | tuple[str, ...] | None = None,
    full_catalog: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """按用户问题收窄 Agent 可见的诊断目录，避免具体问题触发全量巡检。

    ``focus`` 由调用方显式指定要查的诊断项。指标告警归因用的是固定话术，
    里面的"异常进程""进程/服务"会被关键词规则误判成日志和服务诊断项，
    把只有 2 项的预算浪费在最慢的 ``journalctl`` 上，所以这类调用必须显式给 focus。
    """
    if full_catalog is None:
        full_catalog = _full_catalog(device)
    if focus:
        picked = {key: full_catalog[key] for key in focus if key in full_catalog}
        if picked:
            return picked
    text = (question or "").strip().lower()

    # 用户明确要求综合体检时，保留完整目录。
    if any(
        word in text
        for word in ("全面", "整体", "全部", "全量", "综合", "系统健康", "体检")
    ):
        return full_catalog

    groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        (
            (
                "traefik",
                "router",
                "ingress",
                "反向代理",
                "中间件",
                "entrypoint",
                "域名路由",
            ),
            ("traefik_routes", "traefik_runtime"),
        ),
        (
            ("docker", "容器", "compose", "container"),
            (
                "docker_runtime",
                "docker_processes",
                "docker_logs",
                "docker_stats",
                "docker_logs_container",
                "docker_inspect_container",
                "docker_exec_read",
                "docker_images",
                "docker_network_ls",
            ),
        ),
        (
            ("cpu", "处理器", "负载", "load"),
            ("cpu", "load_uptime", "top_processes_cpu"),
        ),
        (("内存", "memory", "mem"), ("memory", "top_processes_mem", "memory_commit")),
        (
            ("磁盘", "disk", "存储", "分区"),
            ("disk", "disk_io", "du_path"),
        ),
        (("文件", "file"), ("read_file", "find_name", "list_dir")),
        (("活跃连接", "连接数", "连接", "established"), ("established_top",)),
        (
            ("端口", "监听", "listen", "port"),
            ("listening_ports", "tcp_probe", "process_list"),
        ),
        (
            ("网络", "网卡", "路由表", "network"),
            ("network", "listening_ports", "tcp_probe"),
        ),
        (
            ("连通", "可达", "telnet", "连不上", "连上", "探测", "ping", "访问"),
            ("tcp_probe", "http_probe", "network"),
        ),
        (
            ("进程", "process"),
            (
                "top_processes_cpu",
                "top_processes_mem",
                "ps_filter",
                "process_list",
                "process_detail",
            ),
        ),
        # 「机器上跑的应用/程序」类盘点:现代机器上应用大多在容器里,容器观测项
        # 直接进目录(2026-10-30 run 782 实测:docker 项全靠补充预算,3 项被
        # docker_runtime/processes/stats 占满,docker_logs 只能被拒,LLM 还试图
        # 借 run_diagnostic 调 custom_read 绕审批)。平台不保证目标真有 Docker,
        # 命令自带守卫+未装短路,无害。
        (
            ("应用", "程序", "跑什么", "在跑", "部署"),
            (
                "process_list",
                "top_processes_cpu",
                "top_processes_mem",
                "ps_filter",
                "running_services",
                "listening_ports",
                "docker_runtime",
                "docker_stats",
                "docker_logs_container",
                "docker_inspect_container",
                "docker_images",
                "docker_network_ls",
                "docker_exec_read",
                "process_detail",
                "list_dir",
            ),
        ),
        (
            ("服务", "service", "systemd"),
            (
                "failed_services",
                "running_services",
                "listening_ports",
                "service_status",
                "service_journal",
                "win_service_status",
            ),
        ),
        (
            ("日志", "错误", "异常", "error", "log"),
            (
                "app_errors",
                "kernel_errors",
                "read_file",
                "win_event_filter",
                "messages_error_count",
            ),
        ),
    ]

    selected: set[str] = set()
    # “路由”单独出现时通常指反向代理路由；明确说“网络路由”则交给网络诊断项。
    if "路由" in text and "网络" not in text:
        selected.add("traefik_routes")
    for keywords, keys in groups:
        if any(keyword in text for keyword in keywords):
            selected.update(key for key in keys if key in full_catalog)

    # 无法识别意图时仍提供完整目录，避免自然语言问题被错误地限制为空。
    if not selected:
        return full_catalog
    return {key: full_catalog[key] for key in full_catalog if key in selected}
