"""Agent 诊断循环 —— OpenAI 兼容 tool-calling。

LLM 能"做"的只有三件事(全部为只读):
  1. list_diagnostics — 列出该设备可用的只读诊断项
  2. run_diagnostic   — 按 key 执行 agent_commands 注册表里的固定命令
  3. get_metrics      — 读本平台已采集的指标趋势(查 DB,不触设备)

边界保证:
  * LLM 永远不接触 shell 自由输入——run_diagnostic 的 key 强制白名单校验,
    幻觉 key 直接拒绝;
  * 每条注册命令在 agent_commands 导入期已过只读校验;
  * 每次诊断的步骤与报告落 agent_runs 审计;
  * 单条输出按 AGENT_MAX_OUTPUT_CHARS 截断,防上下文与内存爆炸。

执行模型:后台线程异步执行,步骤增量落库——前端轮询 /runs/{id}
即可实时看到诊断过程。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from app.config import (
    AGENT_CMD_TIMEOUT,
    AGENT_ENABLED,
    AGENT_LLM_API_KEY,
    AGENT_LLM_BASE_URL,
    AGENT_LLM_MAX_RETRIES,
    AGENT_LLM_MODEL,
    AGENT_LLM_REPORT_TIMEOUT,
    AGENT_MAX_CONCURRENT_RUNS,
    AGENT_MAX_OUTPUT_CHARS,
    AGENT_MAX_STEPS,
    ALERT_ANALYSIS_LLM_TIMEOUT,
)
from app.database import SessionLocal
from app.models.agent_run import AgentRun
from app.models.device import Device, is_windows_os
from app.models.device_metric_sample import DeviceMetricSample
from app.models.pve_guest_binding import PveGuestBinding
from app.services import agent_commands
from app.services.crypto import decrypt
from app.services.pve_guest_status import get_guest_metric_history
from app.services.settings import get_setting
from app.validators import validate_outbound_url

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """LLM 未配置/调用失败/步数超限等。"""


@dataclass
class AgentTarget:
    """Device-compatible runtime target for a PVE guest binding."""

    id: int | None
    name: str
    ip_address: str | None
    os_system: str
    credential_id: int | None = None
    username: str | None = None
    password: str | None = None
    ssh_key: str | None = None
    ssh_port: int = 22
    winrm_port: int = 5985
    ssh_host_key: str | None = None
    type: str = "server"

    @property
    def is_windows(self) -> bool:
        """与 Device.is_windows 同一口径(共享 is_windows_os 真源)。"""
        return is_windows_os(self.os_system)


# 进程重启打断诊断时写进 AgentRun.error 的固定原因。告警自愈据此区分"平台自己
# 把归因掐了"和"归因真跑出了失败结论"，前者要退回未分析重跑，不该报给值班人。
INTERRUPTED_RUN_ERROR = "服务重启，Agent 诊断线程已中断"


def recover_interrupted_runs() -> int:
    """服务重启后回收失去后台线程的 AgentRun 记录。"""
    db = SessionLocal()
    try:
        rows = db.query(AgentRun).filter(AgentRun.status == "running").all()
        for row in rows:
            row.status = "failed"
            row.error = INTERRUPTED_RUN_ERROR
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


# ── 配置解析:DB 设置优先,.env 兜底(系统管理页可改,保存即生效)──


class AgentConfig:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str,
        model: str,
        max_steps: int,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_steps = max_steps

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def ready(self) -> bool:
        return self.enabled and self.configured


def get_agent_config(db: Session) -> AgentConfig:
    """有效配置:DB(agent_*)优先,未设置项回退 .env。API Key 在 DB 中加密存储。"""
    enabled_raw = get_setting(db, "agent_enabled")
    enabled = (enabled_raw.lower() == "true") if enabled_raw else AGENT_ENABLED

    base_url = get_setting(db, "agent_base_url") or AGENT_LLM_BASE_URL

    api_key = ""
    api_key_enc = get_setting(db, "agent_api_key")
    if api_key_enc:
        try:
            api_key = decrypt(api_key_enc)
        except Exception:
            logger.warning("agent_api_key 解密失败,回退 .env")
    if not api_key:
        api_key = AGENT_LLM_API_KEY

    model = get_setting(db, "agent_model") or AGENT_LLM_MODEL

    max_steps_raw = get_setting(db, "agent_max_steps")
    try:
        max_steps = int(max_steps_raw) if max_steps_raw else AGENT_MAX_STEPS
    except ValueError:
        max_steps = AGENT_MAX_STEPS
    max_steps = min(max(max_steps, 1), 20)

    return AgentConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_steps=max_steps,
    )


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_diagnostics",
            "description": "列出服务端已按当前问题筛选的只读诊断项(key 与说明)。目录已在用户上下文中提供,通常无需重复调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnostic",
            "description": "按 key 执行一个只读诊断项,返回命令输出。只能使用 list_diagnostics 返回的 key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "诊断项 key"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metrics",
            "description": "获取平台已采集的服务器性能指标趋势(CPU/内存/磁盘/网络/磁盘 IO 的均值与峰值)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "回看小时数,可选 1/6/24/168",
                        "default": 1,
                    },
                },
                "required": [],
            },
        },
    },
]

_SYSTEM_PROMPT = """你是 DCN 数据中心平台的资深只读诊断 Agent。目标不是展示排查步骤数量,而是用最少、最相关的证据准确回答用户的问题。

硬性规则:
1. 你只能使用提供的三个工具(list_diagnostics / run_diagnostic / get_metrics),它们全部只读。你无法、也不允许修改目标服务器的任何状态。
2. 服务端已在用户上下文中给出与问题相关的诊断目录,通常直接调用 run_diagnostic 即可;不要为了形式重复调用 list_diagnostics。
3. 涉及当前状态、故障原因、配置合理性、性能或安全判断时,必须先取证再回答。不得凭常识猜测目标设备当前状态。
4. 每项结论必须能对应到工具证据。区分“已观测事实”“基于事实的推断”“尚未验证项”,不得把推断写成事实。
5. 命令 exit_code=0 只代表命令执行成功,不代表业务状态正常;必须检查输出内容、空结果、错误文本、时间和数据完整性。
6. run_diagnostic 返回非 0、报错、空结果或截断时,如实说明证据质量;不要反复执行同一项,也不要用无关指标替代缺失证据。
7. 先理解用户真正要判断的对象和标准,再选择最小充分证据集。具体问题只执行直接相关项;只有用户明确要求全面检查时才扩展范围。
8. 对“是否合理/是否正常”类问题,必须先列出判断标准,再逐项对照证据,最后给出正常、存在风险或证据不足的结论。
9. 如果当前工具确实无法覆盖问题,必须具体说明缺少哪类数据、为什么影响判断、下一步应读取什么;禁止只给笼统的“无法确认”。
10. 最终回答前自行复核:是否回答了原问题、是否引用了真实证据、是否遗漏明显矛盾、是否把历史指标误当成当前状态。
11. `get_metrics` 返回的是平台历史采集摘要;必须检查 `latest_sample_at`、`age_seconds` 和 `data_status`。数据过期时不得把它描述为当前实时状态。

范围控制:
12. 用户询问具体对象或单一现象(例如 Traefik 路由、某个服务、某个端口)时,只执行与该对象直接相关的诊断项,最多 2 项;禁止顺便检查 CPU、内存、磁盘、登录记录等无关项目。
13. 只有用户明确要求“全面检查/系统健康/整体排查/全量”时,才允许扩展到多个资源维度。
14. 如果目录中没有与问题直接相关的诊断项,应明确说明无法通过当前诊断项确认,不得用无关指标代替。

最终报告用中文,Markdown 结构:
## 结论
直接回答原问题,给出结论和置信度(高/中/低)。
## 判断依据
列出采用的判断标准及对应证据。
## 排查过程
执行了哪些诊断项,各自的关键发现。
## 发现的问题
按严重程度列出,标注对应证据(来自哪个诊断项)。
## 未验证项
仅列出会实质影响结论、但当前确实缺少的数据;没有则写“无”。
## 建议
可操作的处置建议;涉及修改、重启、清理、创建等变更操作时,必须标注"需人工执行"。容器、服务器、主机、云服务器、虚拟机上的只读查询(进程、日志、状态、配置、资源占用等)你都能通过诊断工具直接完成,一律先自行取证,禁止把可经工具完成的只读检查推给人工。"""


_DOMAIN_GUIDANCE = {
    "traefik_routes": """Traefik 专项规则:
- 路由通常位于业务容器的 Docker Compose labels,不只在 Traefik 容器本身。
- 综合检查 rule、entrypoints、service、middlewares、TLS、目标容器状态、目标端口、Compose project/service、Docker 网络以及已加载 API 路由。
- 重点识别空规则、重复/冲突 Host、过宽 catch-all、service 缺失、端口不匹配、HTTP/HTTPS entrypoint 与 TLS 不自洽、目标容器未运行或不在共享网络。
- 未直接读取 compose.yml 不等于完全无法判断;先利用容器 labels、inspect 和运行时 API 给出能支持的结论。""",
    "docker_runtime": """Docker/Compose 专项规则:
- 区分容器 created/exited/restarting/running 与 health 状态,不要只看容器是否存在。
- 检查 Compose project/service 归属、端口、网络和挂载之间是否自洽;异常容器应指出具体名称和状态。""",
    "docker_processes": """容器进程专项规则:
- pid1 即容器主进程的真实 Entrypoint/Cmd,processes 是运行中容器的进程快照;这就是容器实际运行内容的证据,不要再凭镜像名或端口推断。
- 镜像名为 local/自定义或 PID 1 是 shell 脚本(npm start、entrypoint.sh 等)时,以 processes 里的实际进程为准。
- docker exec 在容器缺少 ps、shell 或权限不足时会报错,如实说明该容器进程不可读,不要推断其内容。""",
    "docker_logs": """容器日志专项规则:
- 输出为每个容器近期日志中的错误/异常行;某容器段为空即代表近期无错误日志,不要推断为异常。
- 结合 docker_runtime 的容器状态一起看:退出/重启中的容器重点看其日志段。""",
}

# 依赖 Docker 运行时的诊断项(命令内部都以 `command -v docker` 守卫)。
# docker_runtime/traefik_routes 是「探测项」本身,不参与门控——它们跑一次
# 就能确认 Docker 是否存在;其余项在其确认不存在后直接短路。
_DOCKER_GATED_KEYS = frozenset(
    {
        "docker_processes",
        "docker_logs",
        "docker_stats",
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
    "docker_stats": "各容器的 CPU/内存/网络 IO 快照;回答“哪个容器在占资源”。",
    "running_services": "当前运行中的 systemd 服务清单;与 failed_services 互补。",
    "event_errors_system": "Windows 系统错误事件。",
    "event_errors_app": "Windows 应用错误事件。",
    "uptime": "Windows 运行时长。",
    "installed_updates": "Windows 最近安装补丁。",
}


def _build_system_prompt(catalog: dict[str, dict]) -> str:
    guidance: list[str] = []
    for key, text in _DOMAIN_GUIDANCE.items():
        if key in catalog and text not in guidance:
            guidance.append(text)
    if not guidance:
        return _SYSTEM_PROMPT
    return _SYSTEM_PROMPT + "\n\n当前问题专项检查规范:\n" + "\n".join(guidance)


# _chat 的 tools 参数哨兵:省略 "tools"/"tool_choice" 字段(与传空数组不同,
# OpenAI 兼容接口会拒绝空数组)。单-pass 归因的结论轮不该再看到任何工具。
_UNSET = object()
# 瞬时错误状态码:重试有意义;其余(4xx 参数/鉴权错误)重试也是同样的错。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 重试退避(秒),按已重试次数取;上限 10s 避免拖爆调用方的等待预算。
_RETRY_BACKOFF = (1.5, 4.0, 10.0)


def _chat(
    messages: list[dict],
    cfg: AgentConfig,
    tools: list[dict] | object = _UNSET,
    *,
    timeout: int | None = None,
    max_retries: int | None = None,
    usage_sink: list | None = None,
) -> dict:
    """一次 chat/completions 调用,返回 message 对象。

    * tools 省略时不发 tools 字段(单-pass 结论轮);交互循环传工具子集。
    * timeout 默认用 ``AGENT_LLM_REPORT_TIMEOUT``(180s):报告轮要产出 3000+
      token,推理型模型实测 55~60s,用常规 60s 贴线跑偶发超时会把
      "22 步证据全部采完"的诊断整个作废。单-pass 归因结论只有 ≤200 字,
      由调用方传入更紧的 ``ALERT_ANALYSIS_LLM_TIMEOUT``。
    * 429/5xx/连接抖动按退避重试(AGENT_LLM_MAX_RETRIES,默认 2 次):
      瞬时错误不再把已采完证据的整轮诊断作废。鉴权/参数类 4xx 不重试。
    """
    url = f"{cfg.base_url}/chat/completions"
    request_timeout = AGENT_LLM_REPORT_TIMEOUT if timeout is None else timeout
    retries = AGENT_LLM_MAX_RETRIES if max_retries is None else max_retries
    payload = {"model": cfg.model, "messages": messages, "temperature": 0.2}
    if tools is not _UNSET:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}

    resp = None
    for attempt in range(retries + 1):
        try:
            validate_outbound_url(cfg.base_url)
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout,
                allow_redirects=False,
            )
        except ValueError as exc:
            # 出站地址不合规是配置错误,重试没有意义
            raise AgentError(str(exc)) from exc
        except requests.RequestException as exc:
            if attempt < retries:
                _log_retry(exc, attempt, retries)
                continue
            raise AgentError(f"LLM 接口连接失败: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS and attempt < retries:
            _log_retry(f"HTTP {resp.status_code}", attempt, retries)
            continue
        break

    if resp.status_code != 200:
        raise AgentError(f"LLM 接口返回 {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise AgentError(f"LLM 响应格式异常: {exc}") from exc
    if usage_sink is not None and isinstance(data.get("usage"), dict):
        usage_sink.append(data["usage"])
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise AgentError(f"LLM 响应格式异常: {exc}") from exc


def _log_retry(reason, attempt: int, retries: int) -> None:
    delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
    logger.warning(
        "LLM 调用瞬时失败(%s),%.1fs 后重试(第 %d/%d 次)",
        reason,
        delay,
        attempt + 1,
        retries,
    )
    time.sleep(delay)


def _resolve_creds(db: Session, device: Device) -> tuple[str, str, str] | None:
    direct_username = getattr(device, "username", None)
    direct_password = getattr(device, "password", None)
    direct_key = getattr(device, "ssh_key", None)
    if direct_username and (direct_password or direct_key):
        return direct_username, direct_password or "", direct_key or ""
    username = getattr(device, "remote_username", None)
    password = (
        decrypt(device.remote_password_enc)
        if getattr(device, "remote_password_enc", None)
        else ""
    )
    ssh_key = (
        decrypt(device.remote_ssh_key_enc)
        if getattr(device, "remote_ssh_key_enc", None)
        else ""
    )
    if not username or (not password and not ssh_key):
        return None
    return username, password, ssh_key


class _SshSession:
    """一次诊断内复用的 SSH 连接(Linux 目标)。

    实测目标机(CentOS 7 / OpenSSH 7.4)每次新建连接的 paramiko 认证阶段要 5~10s
    (服务端 sshd 反查/PAM 阶段耗时)，而诊断命令本身只要 0.1s 左右。归因分析一轮
    通常执行 2 项以上诊断，逐条重连会把整个分析拖成分钟级。

    这里在一轮 ``_loop`` 内复用同一条 transport，把 N 次握手压成 1 次。
    每个诊断运行独占一个实例;交互 ``_loop`` 单线程，单-pass 归因并行
    取证，懒开连接由 ``_open_lock`` 串行(paramiko transport 本身支持
    多线程并发使用)。
    """

    def __init__(self) -> None:
        self._client = None
        self._open_lock = threading.Lock()

    @staticmethod
    def _alive(client) -> bool:
        try:
            transport = client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    def _open(self, db: Session, device, username, password, ssh_key, timeout: int):
        from app.services.ssh import connect_device, open_ssh_client

        if isinstance(device, Device):
            # connect_device 负责 TOFU：首次连上时把 host key 写回设备记录。
            client, _key = connect_device(
                device,
                username,
                password,
                db=db,
                timeout=min(timeout, 15),
                private_key=ssh_key or None,
            )
            return client
        client, _key = open_ssh_client(
            device.ip_address,
            device.ssh_port,
            username,
            password,
            timeout=min(timeout, 15),
            pinned_key_b64=device.ssh_host_key or None,
            private_key=ssh_key or None,
            allow_tofu=not bool(device.ssh_host_key),
        )
        return client

    def exec(
        self,
        db: Session,
        device,
        username: str,
        password: str,
        ssh_key: str,
        command: str,
        timeout: int,
    ) -> tuple[int, str, str]:
        from app.services.ssh import exec_ssh_command

        if self._client is not None and not self._alive(self._client):
            self.close()
        if self._client is None:
            with self._open_lock:
                if self._client is None:
                    self._client = self._open(
                        db, device, username, password, ssh_key, timeout
                    )
        try:
            return exec_ssh_command(self._client, command, timeout=timeout)
        except Exception:
            # 连接可能在两条命令之间被对端回收：丢弃缓存、重建后重试一次。
            self.close()
            self._client = self._open(db, device, username, password, ssh_key, timeout)
            return exec_ssh_command(self._client, command, timeout=timeout)

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            logger.debug("close reused SSH client failed", exc_info=True)


def _exec_diagnostic(
    db: Session,
    device: Device,
    command: str,
    timeout: int,
    session: "_SshSession | None" = None,
) -> tuple[int, str, str]:
    """经设备通道执行注册表命令(Windows→WinRM,Linux→SSH)。"""
    creds = _resolve_creds(db, device)
    if creds is None:
        raise AgentError("设备未绑定有效凭据,无法执行诊断")
    username, password, ssh_key = creds

    if device.is_windows:
        from app.services.winrm import run_on_device

        if isinstance(device, Device):
            return run_on_device(device, username, password, command, timeout=timeout)
        from app.services.winrm import run_powershell

        return run_powershell(
            device.ip_address,
            username,
            password,
            command,
            port=device.winrm_port,
            timeout=timeout,
        )

    if session is not None:
        return session.exec(db, device, username, password, ssh_key, command, timeout)

    from app.services.ssh import exec_on_device, exec_ssh_command, open_ssh_client

    if isinstance(device, Device):
        return exec_on_device(
            device,
            username,
            password,
            command,
            timeout=timeout,
            db=db,
            private_key=ssh_key or None,
        )
    client, _key = open_ssh_client(
        device.ip_address,
        device.ssh_port,
        username,
        password,
        timeout=min(timeout, 15),
        pinned_key_b64=device.ssh_host_key or None,
        private_key=ssh_key or None,
        allow_tofu=not bool(device.ssh_host_key),
    )
    try:
        return exec_ssh_command(client, command, timeout=timeout)
    finally:
        client.close()


def _diagnostic_output(exit_code: int, out: str, err: str) -> str:
    """挑出该交给 LLM 的那一段输出。

    成功时只用 stdout。旧写法 ``out or err`` 在 stdout 为空时回退到 stderr，
    而 stderr 是 PowerShell 的告警/进度流。对诊断而言「命令成功且无输出」
    本身就是结论(例如近 24h 无服务失败事件)，回退之后模型读到的是几百字符
    CLIXML 噪音，得先自己看懂那段 XML 才能得出「无异常」——白耗 token、
    且结论靠运气。exit_code 已经能区分成败，失败时才需要 stderr 当错误信息。
    """
    if exit_code == 0:
        return out or ""
    return err or out or ""


def _metrics_summary(db: Session, device: Device | AgentTarget, hours: int) -> str:
    """近 N 小时指标的紧凑摘要(给 LLM 的上下文,不是完整时序)。

    物理设备读 ``DeviceMetricSample``;虚拟机(AgentTarget,id=None)没有 ORM
    指标记录,改用 PVE 快照环内存历史(经运维接入绑定按 IP 反查宿主身份)。
    """
    device_id = getattr(device, "id", None)
    if device_id is not None:
        return _device_metrics_summary(db, device_id, hours)
    if not device.ip_address:
        return json.dumps(
            {"note": "目标无 IP,无法关联平台采集数据"}, ensure_ascii=False
        )
    binding = (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.ip_address == device.ip_address.strip(),
            PveGuestBinding.enabled == 1,
        )
        .first()
    )
    if binding is None:
        return json.dumps(
            {"note": "PVE 虚拟机尚未接入平台指标采集,请使用虚拟机内诊断项取证"},
            ensure_ascii=False,
        )
    return _guest_metrics_summary(binding.connection_id, binding.vmid, hours)


def _device_metrics_summary(db: Session, device_id: int, hours: int) -> str:
    """物理设备:DeviceMetricSample 的窗口统计。"""
    hours = min(max(hours, 1), 168)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        db.query(DeviceMetricSample)
        .filter(
            DeviceMetricSample.device_id == device_id,
            DeviceMetricSample.ts >= since,
        )
        .order_by(DeviceMetricSample.ts)
        .all()
    )
    if not rows:
        return json.dumps({"note": "该时间范围内无采集数据"}, ensure_ascii=False)

    latest_ts = rows[-1].ts
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - latest_ts).total_seconds()))

    def _stat(field: str) -> dict | None:
        vals = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        if not vals:
            return None
        return {
            "latest": round(vals[-1], 1),
            "avg": round(sum(vals) / len(vals), 1),
            "max": round(max(vals), 1),
        }

    summary = {
        "window_hours": hours,
        "samples": len(rows),
        "latest_sample_at": latest_ts.isoformat(),
        "age_seconds": age_seconds,
        "data_status": "stale" if age_seconds > 180 else "fresh",
        "cpu_pct": _stat("cpu_pct"),
        "mem_pct": _stat("mem_pct"),
        "disk_max_pct": _stat("disk_max_pct"),
        "load1": _stat("load1"),
        "net_rx_bps": _stat("net_rx_bps"),
        "net_tx_bps": _stat("net_tx_bps"),
        "disk_read_bps": _stat("disk_read_bps"),
        "disk_write_bps": _stat("disk_write_bps"),
    }
    return json.dumps(summary, ensure_ascii=False)


def _guest_metrics_summary(connection_id: int, vmid: int, hours: int) -> str:
    """虚拟机:PVE 快照环历史的窗口统计(cpu/mem 与告警评估同口径)。

    磁盘使用率快照里没有(告警评估也不产出),不编造字段。
    """
    samples = get_guest_metric_history(connection_id, vmid)
    if not samples:
        return json.dumps(
            {"note": "PVE 快照历史尚为空(采集刚启动或本实例非 leader)"},
            ensure_ascii=False,
        )
    hours = min(max(hours, 1), 168)
    cutoff = time.time() - hours * 3600
    rows = [s for s in samples if float(s.get("ts") or 0) >= cutoff]
    if not rows:
        return json.dumps({"note": "该时间范围内无采集数据"}, ensure_ascii=False)

    latest = rows[-1]
    latest_ts = float(latest.get("ts") or 0)

    cpu_vals = [round(float(s.get("cpu") or 0) * 100.0, 1) for s in rows]

    def _pct(field: str, total_field: str) -> dict | None:
        vals = [
            round(float(s.get(field) or 0) / float(s.get(total_field) or 0) * 100.0, 1)
            for s in rows
            if s.get(total_field)
        ]
        if not vals:
            return None
        return {
            "latest": vals[-1],
            "avg": round(sum(vals) / len(vals), 1),
            "max": round(max(vals), 1),
        }

    summary = {
        "window_hours": hours,
        "samples": len(rows),
        "latest_sample_at": datetime.fromtimestamp(
            latest_ts, tz=timezone.utc
        ).isoformat(),
        "age_seconds": max(0, int(time.time() - latest_ts)),
        "data_status": "stale" if time.time() - latest_ts > 180 else "fresh",
        "status": latest.get("status"),
        "cpu_pct": (
            {
                "latest": cpu_vals[-1],
                "avg": round(sum(cpu_vals) / len(cpu_vals), 1),
                "max": round(max(cpu_vals), 1),
            }
            if cpu_vals
            else None
        ),
        "mem_pct": _pct("mem", "maxmem"),
    }
    return json.dumps(summary, ensure_ascii=False)


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
        full_catalog = agent_commands.get_diagnostics_for_device(device)
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
            ("docker_runtime", "docker_processes", "docker_logs", "docker_stats"),
        ),
        (
            ("cpu", "处理器", "负载", "load"),
            ("cpu", "load_uptime", "top_processes_cpu"),
        ),
        (("内存", "memory", "mem"), ("memory", "top_processes_mem")),
        (("磁盘", "disk", "存储", "分区"), ("disk", "disk_io")),
        (("活跃连接", "连接数", "连接", "established"), ("established_top",)),
        (("端口", "监听", "listen", "port"), ("listening_ports",)),
        (("网络", "网卡", "路由表", "network"), ("network", "listening_ports")),
        (
            ("服务", "service", "systemd"),
            ("failed_services", "running_services", "listening_ports"),
        ),
        (("日志", "错误", "异常", "error", "log"), ("app_errors", "kernel_errors")),
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


def _dispatch(
    db: Session,
    device: Device,
    catalog: dict,
    call: dict,
    steps: list[dict],
    executed_keys: set[str] | None = None,
    diagnostic_budget: int | None = None,
    metrics_allowed: bool = True,
    session: _SshSession | None = None,
) -> str:
    """执行一个 tool_call 并返回 tool 消息内容。key 白名单校验在这里。"""
    name = call.get("function", {}).get("name", "")
    raw_args = call.get("function", {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        args = {}

    if name == "list_diagnostics":
        out = [
            {
                "key": k,
                "label": v["label"],
                "hint": _DIAGNOSTIC_HINTS.get(k, "只读诊断快照,需结合问题语境解释。"),
            }
            for k, v in catalog.items()
        ]
        return json.dumps(out, ensure_ascii=False)

    if name == "get_metrics":
        if not metrics_allowed:
            steps.append(
                {
                    "tool": "get_metrics",
                    "ok": False,
                    "error": "当前问题不是性能指标查询,已拒绝无关指标读取",
                }
            )
            return json.dumps(
                {"error": "当前问题不是性能指标查询,请勿读取 CPU/内存/磁盘等无关指标"},
                ensure_ascii=False,
            )
        try:
            hours = int(args.get("hours") or 1)
        except (TypeError, ValueError):
            hours = 1
        steps.append({"tool": "get_metrics", "hours": hours, "ok": True})
        return _metrics_summary(db, device, hours)

    if name == "run_diagnostic":
        key = str(args.get("key") or "")
        item = catalog.get(key)
        if item is None:
            # LLM 幻觉出的 key / 试图执行注册表外的命令——拒绝并记录
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "ok": False,
                    "error": "诊断项不存在,已拒绝(只读边界)",
                }
            )
            return json.dumps(
                {
                    "error": f"诊断项 {key!r} 不存在,请从 list_diagnostics 返回的 key 中选择"
                },
                ensure_ascii=False,
            )
        if executed_keys is not None and key in executed_keys:
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": "该诊断项已执行,拒绝重复执行",
                }
            )
            return json.dumps(
                {"key": key, "error": "该诊断项已执行,无需重复执行"},
                ensure_ascii=False,
            )
        if (
            diagnostic_budget is not None
            and executed_keys is not None
            and len(executed_keys) >= diagnostic_budget
        ):
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": f"具体问题最多执行 {diagnostic_budget} 项相关诊断",
                }
            )
            return json.dumps(
                {
                    "key": key,
                    "error": f"具体问题最多执行 {diagnostic_budget} 项相关诊断",
                },
                ensure_ascii=False,
            )
        if executed_keys is not None:
            executed_keys.add(key)
        # Docker 依赖门控:docker_runtime(或任一 docker 前置项)已确认目标
        # 未安装 Docker 时,后续 docker/traefik 系列诊断项必然也拿不到数据
        # (每个命令都自带 `command -v docker` 守卫并输出「未安装 Docker」),
        # 直接短路拒绝,省掉 LLM 每项一次往返与目标机执行开销。
        # 跳过项不写入 steps:执行明细只列真实执行过的项,用户已经从
        # 探测项的「未安装 Docker」知道了后续为什么没有容器数据;
        # LLM 侧仍会在 tool 结果里看到同样的说明。
        if key in _DOCKER_GATED_KEYS and "未安装 Docker" in _docker_absent_note(steps):
            return json.dumps(
                {
                    "key": key,
                    "error": "目标未安装 Docker,该项依赖容器运行时,无需执行;请基于已有证据给出结论。",
                },
                ensure_ascii=False,
            )
        try:
            exit_code, out, err = _exec_diagnostic(
                db,
                device,
                item["command"],
                min(item["timeout"], AGENT_CMD_TIMEOUT),
                session=session,
            )
        except Exception as exc:
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": str(exc)[:200],
                }
            )
            return json.dumps({"key": key, "error": str(exc)[:300]}, ensure_ascii=False)

        raw_text = _diagnostic_output(exit_code, out, err)
        truncated = len(raw_text) > AGENT_MAX_OUTPUT_CHARS
        text = raw_text[:AGENT_MAX_OUTPUT_CHARS]
        steps.append(
            {
                "tool": "run_diagnostic",
                "key": key,
                "label": item["label"],
                "ok": exit_code == 0,
                "preview": text[:300],
                "truncated": truncated,
            }
        )
        return json.dumps(
            {
                "key": key,
                "exit_code": exit_code,
                "output": text,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


def _persist_steps(run_id: int, steps: list[dict]) -> None:
    """增量落库当前步骤(供前端轮询实时进度)。"""
    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if row:
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            db.commit()
    except Exception:
        logger.debug("persist steps failed for run %s", run_id, exc_info=True)
        db.rollback()
    finally:
        db.close()


def _append_usage_step(steps: list[dict], usage: list[dict], cfg) -> None:
    """把本轮 LLM 用量记进步骤尾部(agent_runs 审计可见,无需额外表结构)。"""
    if not usage:
        return
    prompt_tokens = sum(int(u.get("prompt_tokens") or 0) for u in usage)
    completion_tokens = sum(int(u.get("completion_tokens") or 0) for u in usage)
    steps.append(
        {
            "tool": "llm_usage",
            "label": "LLM 用量",
            "ok": True,
            "preview": (
                f"{cfg.model} · {len(usage)} 次调用 · "
                f"prompt {prompt_tokens} tok · completion {completion_tokens} tok"
            ),
        }
    )


def _dispatch_calls(
    db: Session,
    device,
    catalog: dict,
    tool_calls: list[dict],
    steps: list[dict],
    executed_keys: set[str] | None,
    diagnostic_budget: int | None,
    metrics_allowed: bool,
    session: "_SshSession | None",
    messages: list[dict],
    on_step,
) -> None:
    """执行一整轮 tool_call 并按原顺序回填 tool 消息。

    模型经常在一轮里同时点多个诊断项;串行执行时 Wall time = 各命令耗时之和,
    并行(共享同一条 SSH 连接,paramiko 支持并发 channel;WinRM 本就无状态)
    后变成取最大值。同一轮里重复点名的 key 只执行第一个,其余等批次结束后
    走已有的「已执行拒绝」路径,避免并行下 check-then-add 竞态重复执行。
    """
    if len(tool_calls) == 1:
        result = _dispatch(
            db,
            device,
            catalog,
            tool_calls[0],
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
        )
        if on_step:
            on_step(steps)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_calls[0].get("id", ""),
                "content": result,
            }
        )
        return

    unique: list[tuple[int, dict]] = []
    dups: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for idx, call in enumerate(tool_calls):
        name = call.get("function", {}).get("name", "")
        raw_args = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        key = str(args.get("key") or "") if name == "run_diagnostic" else ""
        if key and key in seen:
            dups.append((idx, call))
        else:
            if key:
                seen.add(key)
            unique.append((idx, call))

    results: dict[int, str] = {}

    def _one(item: tuple[int, dict]) -> tuple[int, str]:
        idx, call = item
        content = _dispatch(
            db,
            device,
            catalog,
            call,
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
        )
        return idx, content

    with ThreadPoolExecutor(max_workers=min(4, len(unique))) as pool:
        for fut in as_completed([pool.submit(_one, item) for item in unique]):
            idx, content = fut.result()
            results[idx] = content
            if on_step:
                on_step(steps)
    for idx, call in dups:
        results[idx] = _dispatch(
            db,
            device,
            catalog,
            call,
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
        )
        if on_step:
            on_step(steps)
    for idx, call in enumerate(tool_calls):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": results[idx],
            }
        )


def _loop(
    db: Session,
    device: Device | AgentTarget,
    question: str,
    steps: list[dict],
    cfg: AgentConfig,
    on_step=None,
    focus: list[str] | tuple[str, ...] | None = None,
) -> str:
    full_catalog = agent_commands.get_diagnostics_for_device(device)
    catalog = _select_catalog(device, question, focus, full_catalog)
    os_label = "Windows" if device.is_windows else "Linux"

    user_ctx = (
        f"目标设备:{device.name}(IP: {device.ip_address or '未知'},"
        f"类型: {device.type},系统: {device.os_system or os_label})。\n"
        f"用户问题:{question}\n"
        "请开始只读排查。"
    )
    messages = [
        {"role": "system", "content": _build_system_prompt(catalog)},
        {"role": "user", "content": user_ctx},
    ]
    selected_keys = (
        "；".join(
            f"{key}（{_DIAGNOSTIC_HINTS.get(key, '只读诊断快照')}）" for key in catalog
        )
        or "无"
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"本轮服务端筛选出的相关诊断项为: {selected_keys}。\n"
                "请优先使用这些诊断项取证;不要自行扩大到目录之外。"
            ),
        }
    )
    executed_keys: set[str] = set()
    # 具体问题默认最多 2 项诊断;目录自带 3 项以上的分组(如 Docker 组 4 项),
    # 预算取下限,保证同组取证能跑完,又不放任全量体检。
    diagnostic_budget = None if catalog is full_catalog else max(2, len(catalog))
    metric_keywords = (
        "cpu",
        "内存",
        "memory",
        "磁盘",
        "disk",
        "网络",
        "指标",
        "性能",
        "负载",
    )
    metrics_allowed = catalog is full_catalog or any(
        word in question.lower() for word in metric_keywords
    )
    available_tools = (
        _TOOLS
        if metrics_allowed
        else [tool for tool in _TOOLS if tool["function"]["name"] != "get_metrics"]
    )
    # 目录(含 key/标签/hint)已注入用户上下文,list_diagnostics 只会诱导模型
    # 多花一轮 LLM 往返去问它已经知道的答案,一律不提供。_dispatch 的处理分支
    # 保留,兼容旧客户端/测试直接调用的场景。
    available_tools = [
        tool
        for tool in available_tools
        if tool["function"]["name"] != "list_diagnostics"
    ]
    usage: list[dict] = []
    evidence_retry = False

    # Linux 目标整轮复用一条 SSH 连接：握手要 5~10s，而单条诊断命令只要 0.1s。
    # Windows 走 WinRM 无状态请求，没有可复用的会话。
    session = None if device.is_windows else _SshSession()
    try:
        for _ in range(cfg.max_steps):
            msg = _chat(messages, cfg, available_tools, usage_sink=usage)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                if not any(step.get("ok") for step in steps) and not evidence_retry:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.get("content"),
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "你还没有取得有效运行时证据。请先调用一个最相关的只读诊断工具，再给出结论。",
                        }
                    )
                    evidence_retry = True
                    continue
                _append_usage_step(steps, usage, cfg)
                return msg.get("content") or "(模型未返回内容)"

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            _dispatch_calls(
                db,
                device,
                catalog,
                tool_calls,
                steps,
                executed_keys,
                diagnostic_budget,
                metrics_allowed,
                session,
                messages,
                on_step,
            )

        raise AgentError(f"超过最大诊断步数({cfg.max_steps}),已中止")
    finally:
        if session is not None:
            session.close()


def _focus_call(key: str) -> dict:
    """单-pass 归因合成 run_diagnostic tool_call(复用 _dispatch 的白名单校验)。"""
    return {
        "id": f"focus-{key}",
        "function": {"name": "run_diagnostic", "arguments": json.dumps({"key": key})},
    }


_SINGLE_PASS_NOTE = (
    "\n\n[单-pass 归因]以上运行时证据已由服务端并行采集完毕，不要再请求任何工具。"
    "请基于证据直接判断归因(业务增长、异常进程、资源泄漏等)。"
    "输出要求:只输出一段 ≤200 字的结论，按「归因 + 关键证据 + 处置建议」三段式，"
    "不要铺陈、不要逐条复述证据、不要标题。"
)


def _container_snapshot_evidence(db: Session, device, max_age: int = 180) -> str | None:
    """容器采集器(60s 一批)的最新容器资源快照;缺失/过期返回 None，调用方回退现跑命令。"""
    from app.models.device_container import DeviceContainer
    from app.models.pve_guest_binding import PveGuestBinding

    if getattr(device, "id", None):
        rows = (
            db.query(DeviceContainer)
            .filter(DeviceContainer.device_id == device.id)
            .all()
        )
    else:
        binding = (
            db.query(PveGuestBinding)
            .filter(
                PveGuestBinding.ip_address == device.ip_address,
                PveGuestBinding.enabled == 1,
            )
            .first()
        )
        if binding is None:
            return None
        rows = (
            db.query(DeviceContainer)
            .filter(DeviceContainer.pve_guest_binding_id == binding.id)
            .all()
        )
    if not rows:
        return None
    stamps = [r.updated_at for r in rows if r.updated_at]
    if not stamps:
        return None
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - newest).total_seconds() > max_age:
        return None

    def _fmt(value) -> str:
        return "-" if value is None else f"{value}"

    return "\n".join(
        f"{r.name} state={r.state or '-'} cpu={_fmt(r.cpu_pct)}% mem={_fmt(r.mem_pct)}%"
        for r in sorted(rows, key=lambda row: row.name)
    )


def _platform_trend_evidence(db: Session, device) -> str | None:
    """平台已采集的指标趋势(零成本证据):设备=采集样本,虚拟机=PVE 快照历史。

    区分「业务增长」与「突发泄漏」靠趋势,不靠一次快照;这份证据平台自己就有,
    不必让目标机再跑命令。无数据时返回 None(不往上下文里塞噪音)。
    """
    try:
        summary = _metrics_summary(db, device, 1)
    except Exception:
        logger.debug("platform trend evidence failed", exc_info=True)
        return None
    if not summary or '"note"' in summary:
        return None
    return summary


def run_focus_diagnosis(
    db: Session,
    device: Device | AgentTarget,
    question: str,
    steps: list[dict],
    cfg: AgentConfig,
    focus: list[str] | tuple[str, ...],
    on_step=None,
) -> str:
    """告警归因专用的单-pass 诊断;手动诊断/自动化 agent 的交互 _loop 不受影响。

    归因的取证项已由调用方按指标精确锁定(_ANALYSIS_FOCUS)，交互循环里 LLM
    那几轮「选项」是纯浪费:这里跳过全部选择轮，取证并行执行(Linux 复用同一条
    SSH 连接)，然后仅一次 LLM 调用出结论。系统提示只含 focus 子集(天然瘦身);
    docker_stats 优先复用容器采集器快照，缺失时回退现跑命令。
    结论轮不带任何工具(协议上就发不了 tool_call,不会再出现"模型违反提示去
    调工具→content 为空→报告变成占位符"的情况),超时用归因专用的更紧预算。
    """
    full_catalog = agent_commands.get_diagnostics_for_device(device)
    catalog = {key: full_catalog[key] for key in focus if key in full_catalog}
    if not catalog:
        raise AgentError("归因取证项为空或目标平台不具备其中任何一项")
    snapshot = _container_snapshot_evidence(db, device)
    use_snapshot = snapshot is not None and "docker_stats" in catalog
    keys = [k for k in catalog if not (k == "docker_stats" and use_snapshot)]

    results: list[tuple[str, str]] = []
    session = None if device.is_windows else _SshSession()
    try:

        def _one(key: str):
            local: list[dict] = []
            content = _dispatch(
                db, device, catalog, _focus_call(key), local, session=session
            )
            return key, content, local

        with ThreadPoolExecutor(max_workers=min(4, max(len(keys), 1))) as pool:
            for fut in as_completed([pool.submit(_one, k) for k in keys]):
                key, content, local = fut.result()
                steps.extend(local)
                results.append((key, content))
                if on_step:
                    on_step(steps)
    finally:
        if session is not None:
            session.close()
    if use_snapshot:
        steps.append(
            {
                "tool": "run_diagnostic",
                "key": "docker_stats",
                "label": "容器资源快照(复用采集器快照)",
                "ok": True,
                "preview": (snapshot or "")[:2000],
            }
        )
        results.append(("docker_stats", snapshot or ""))
        if on_step:
            on_step(steps)
    order = {key: idx for idx, key in enumerate(catalog)}
    results.sort(key=lambda item: order.get(item[0], 99))
    evidence = "\n\n".join(
        f"## {catalog[key].get('label', key)}({key})\n{content}"
        for key, content in results
    )
    trend = _platform_trend_evidence(db, device)
    if trend:
        # 平台自己的采集数据:先看趋势再归因,能区分持续增长与突发泄漏。
        evidence = f"## 平台指标趋势(近 1 小时,平台采集)\n{trend}\n\n{evidence}"
    messages = [
        {
            "role": "system",
            "content": _build_system_prompt(catalog) + _SINGLE_PASS_NOTE,
        },
        {
            "role": "user",
            "content": (
                f"{question}\n\n运行时证据已采集完毕，如下:\n{evidence}\n\n"
                "请直接归因并给出处置建议。"
            ),
        },
    ]
    usage: list[dict] = []
    msg = _chat(
        messages,
        cfg,
        timeout=ALERT_ANALYSIS_LLM_TIMEOUT,
        max_retries=1,
        usage_sink=usage,
    )
    _append_usage_step(steps, usage, cfg)
    if on_step:
        on_step(steps)
    return msg.get("content") or "(模型未返回内容)"


# 诊断运行完成事件:run_id -> Event。_execute_run 落完终态后 set,
# 等待方(告警归因)用事件唤醒代替秒级轮询,几十次 DB 会话缩成 1~2 次。
_run_events: dict[int, threading.Event] = {}
_run_events_lock = threading.Lock()
# 同时执行的诊断运行数上限(批量诊断/告警归因集中触发时排队,
# 防止把目标机 SSH 连接与 LLM 配额同时打满)。
_run_slots = threading.BoundedSemaphore(AGENT_MAX_CONCURRENT_RUNS)


def _register_run_event(run_id: int) -> threading.Event:
    event = threading.Event()
    with _run_events_lock:
        _run_events[run_id] = event
    return event


def wait_run_completion(run_id: int, timeout: float) -> bool:
    """等待运行落到终态(事件在终态落库后 set)。返回是否被事件唤醒;
    False 表示超时或该运行不是本进程创建的(调用方自行回退轮询)。"""
    with _run_events_lock:
        event = _run_events.get(run_id)
    if event is None:
        return False
    return event.wait(timeout)


def start_diagnosis(
    device: Device,
    question: str,
    user_id: int,
    cfg: AgentConfig | None = None,
    focus: list[str] | tuple[str, ...] | None = None,
    single_pass: bool = False,
) -> int:
    """创建运行记录、后台线程执行诊断,立即返回 run_id(前端轮询进度)。"""
    if cfg is None:
        db0 = SessionLocal()
        try:
            cfg = get_agent_config(db0)
        finally:
            db0.close()

    db = SessionLocal()
    try:
        run = AgentRun(
            user_id=user_id,
            device_id=device.id,
            device_name=device.name,
            device_ip=device.ip_address,
            question=question[:1000],
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    _register_run_event(run_id)
    threading.Thread(
        target=_execute_run,
        args=(run_id, device, question, cfg, focus, single_pass),
        daemon=True,
        name=f"agent-run-{run_id}",
    ).start()
    return run_id


def _execute_run(
    run_id: int,
    device: Device | AgentTarget,
    question: str,
    cfg: AgentConfig,
    focus: list[str] | tuple[str, ...] | None = None,
    single_pass: bool = False,
) -> None:
    """后台线程:执行诊断循环,步骤增量落库,结束写终态。

    执行体包在全局信号量里(AGENT_MAX_CONCURRENT_RUNS):批量诊断时排队,
    避免几十条线程同时握手 SSH + 打 LLM。终态落库后 set 完成事件。
    """
    started = time.time()
    steps: list[dict] = []

    status = "completed"
    report = None
    error = None
    try:
        with _run_slots:
            db = SessionLocal()
            try:
                if getattr(device, "id", None) is None:
                    # 虚拟机等运行时目标(AgentTarget)没有 ORM 身份，凭据在内存里;
                    # 按 id 回查 Device 表必然落空并报「设备不存在」。
                    target = device
                else:
                    target = db.query(Device).filter(Device.id == device.id).first()
                    if target is None:
                        raise AgentError("设备不存在")

                def on_step(current_steps) -> None:
                    _persist_steps(run_id, current_steps)

                if single_pass:
                    report = run_focus_diagnosis(
                        db, target, question, steps, cfg, focus or (), on_step=on_step
                    )
                else:
                    report = _loop(
                        db,
                        target,
                        question,
                        steps,
                        cfg,
                        on_step=on_step,
                        focus=focus,
                    )
            finally:
                db.close()
    except Exception as exc:
        logger.warning("Agent diagnosis failed for run %s: %s", run_id, exc)
        status = "failed"
        error = str(exc)[:500]

    duration_ms = int((time.time() - started) * 1000)

    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if row:
            row.status = status
            row.report = report
            row.error = error
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.duration_ms = duration_ms
            db.commit()
    finally:
        db.close()
        # 终态已提交,唤醒等待方;事件对象从注册表摘掉(等待方手里已持有引用,
        # dict 里留着只会泄漏)。
        with _run_events_lock:
            event = _run_events.pop(run_id, None)
        if event:
            event.set()


def run_diagnosis(
    device: Device | AgentTarget,
    question: str,
    user_id: int,
    cfg: AgentConfig | None = None,
    on_step=None,
    audit_device_id: int | None = None,
    focus: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """同步执行一次完整诊断(测试/内部调用用;API 走 start_diagnosis 异步)。

    cfg 为空时自行从 DB/.env 解析有效配置。
    """
    started = time.time()
    steps: list[dict] = []

    if cfg is None:
        db0 = SessionLocal()
        try:
            cfg = get_agent_config(db0)
        finally:
            db0.close()

    db = SessionLocal()
    run = AgentRun(
        user_id=user_id,
        device_id=audit_device_id if audit_device_id is not None else device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question=question[:1000],
        status="running",
        created_at=datetime.now(timezone.utc),
    )
    try:
        db.add(run)
        db.commit()
        db.refresh(run)
    finally:
        db.close()

    status = "completed"
    report = None
    error = None
    try:
        db2 = SessionLocal()
        try:
            report = _loop(
                db2, device, question, steps, cfg, on_step=on_step, focus=focus
            )
        finally:
            db2.close()
    except Exception as exc:
        logger.warning("Agent diagnosis failed for device %s: %s", device.id, exc)
        status = "failed"
        error = str(exc)[:500]

    duration_ms = int((time.time() - started) * 1000)

    db3 = SessionLocal()
    try:
        row = db3.query(AgentRun).filter(AgentRun.id == run.id).first()
        if row:
            row.status = status
            row.report = report
            row.error = error
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.duration_ms = duration_ms
            db3.commit()
    finally:
        db3.close()

    return {
        "run_id": run.id,
        "status": status,
        "report": report,
        "error": error,
        "steps": steps,
        "duration_ms": duration_ms,
    }
