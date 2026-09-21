"""告警自愈:离线目标自动拉起 + 指标过高的 Agent 归因分析。

触发点都在 services/alerts.py 的评估事务提交之后(见 submit_*):
  * host_status      → 目标是 PVE 虚拟机/LXC 就下发 start;物理机无法远程上电，标记 skipped;
  * container_status → docker start(复用容器管理的目标解析与命令通道，并写审计);
  * cpu/mem/disk     → 指标持续超阈值后调 Agent 做只读诊断，把结论写回告警事件。

处置进度以"话术"形式拼进 alert_events.message(基础描述 +当前动作 + 分析结论)，
所以 Webhook 推送和告警中心看到的是同一条随时间推进的消息:
    主机 X 当前已离线或无法连接
    → …；已识别为 PVE 虚拟机 [IT-PVE] QEMU 101 web-01，正在下发启动指令…(第 1/2 次)
    → …；启动指令已下发，正在确认目标是否恢复运行…
    → …；自动拉起操作成功，目标已恢复运行

所有动作都受闸门约束(最大尝试次数 + 冷却时间 + 同一事件单飞)，避免把反复启动
做成 boot loop;后台线程池分处置/分析两组，长耗时的 LLM 诊断不会堵住自动拉起。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable

from sqlalchemy.orm import Session

from app.config import (
    ALERT_ANALYSIS_HOLD_NOTIFICATION,
    ALERT_ANALYSIS_HOLD_TIMEOUT,
    ALERT_ANALYSIS_MAX_STEPS,
    ALERT_ANALYSIS_SUSTAIN_SECONDS,
    ALERT_ANALYSIS_WAIT_SECONDS,
    ALERT_AUTO_ANALYSIS,
    ALERT_AUTO_REMEDIATION,
    ALERT_REMEDIATION_COOLDOWN,
    ALERT_REMEDIATION_MAX_ATTEMPTS,
    ALERT_REMEDIATION_VERIFY_SECONDS,
)
from app.database import SessionLocal
from app.models.agent_run import AgentRun
from app.models.alert import AlertEvent
from app.models.device import Device
from app.models.device_container import ContainerAction, DeviceContainer
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.services.agent import (
    INTERRUPTED_RUN_ERROR,
    AgentTarget,
    get_agent_config,
    start_diagnosis,
)
from app.services.automation import _pve_runtime_device
from app.services.containers_collector import (
    decode_pve_target_id,
    load_container_target,
    run_container_action,
)
from app.services.pve import build_client, is_guest_template
from app.validators import validate_container_name

logger = logging.getLogger(__name__)

# 可自动拉起 / 可 AI 归因的告警指标
REMEDIATION_METRICS = {"host_status", "container_status"}
# 容器状态告警同样归因:容器挂了问「为什么挂」(退出码/日志/进程),取证项
# 已按 container_status 锁定;曾经只在 _ANALYSIS_FOCUS 里写了组合却没接进闸门,
# 是死配置。
ANALYSIS_METRICS = {"cpu_pct", "mem_pct", "disk_max_pct", "container_status"}
# 自愈进度跃迁时额外推送的 Webhook 事件名(不覆盖告警自身的通知状态)
REMEDIATION_WEBHOOK_EVENT = "alert.remediation"
# 归因结论独立事件:告警卡没等到结论(宽限放行)时，归因完成后用它补发一张。
ANALYSIS_WEBHOOK_EVENT = "alert.analysis"

REMEDIATION_STATE_LABELS = {
    "": "未处置",
    "running": "自动处置中",
    "verifying": "等待恢复确认",
    "succeeded": "处置成功",
    "failed": "处置失败",
    "skipped": "无法自动处置",
}
ANALYSIS_STATE_LABELS = {
    "": "未分析",
    "running": "AI 归因中",
    "completed": "AI 归因完成",
    "failed": "AI 归因失败",
    "skipped": "未启用 Agent",
}

# 写回告警事件的分析结论上限(MySQL TEXT 64KB，留出余量)
_ANALYSIS_MAX_CHARS = 8000
_VERIFY_INTERVAL = {"pve_guest": 5, "container": 10}

# 指标告警归因该取哪些证据，由指标本身决定，不交给关键词去猜。
# 归因话术是固定模板，里面的"异常进程""进程/服务"会被 _select_catalog 的关键词
# 规则误命中日志组(app_errors/kernel_errors)和服务组(failed_services/
# listening_ports)，把只有 2 项的诊断预算浪费在与归因无关、又最慢的
# `journalctl --since '24 hours ago'` 上。这里按指标直接锁定取证项，
# 顺序即优先级：第一项就是回答"谁在占用"所必需的证据。
_ANALYSIS_FOCUS: dict[str, tuple[str, ...]] = {
    # 指标占用归因:谁在占(主机进程 + 容器进程 + 容器资源快照) + 资源现状。
    "cpu_pct": ("top_processes_cpu", "cpu", "docker_processes", "docker_stats"),
    "mem_pct": ("top_processes_mem", "memory", "docker_processes", "docker_stats"),
    # 磁盘归因要能回答「哪个目录在涨」:分区级 df 之外补目录级 du。
    "disk_max_pct": ("disk", "disk_usage_top", "disk_io"),
    # 容器状态告警:容器状态 + 里面跑的是什么 + 近期错误日志。
    "container_status": ("docker_runtime", "docker_processes", "docker_logs"),
}

# 轮询 Agent 运行记录的间隔(秒)。正常路径由完成事件唤醒(见 _wait_for_run),
# 这里的间隔只用于事件缺失时的兑底轮询(旧进程创建的 run 等)。
_ANALYSIS_POLL_INTERVAL = 1.0

_heal_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert-heal")
# 归因提交后全程等结论,worker 被占用直到终态;多台同时告警时第 3 台就要排队,
# 排队期间扣住的通知照样超时,提到 4 让集中告警不至于排队。
_analysis_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-analysis")
_heal_inflight: set[int] = set()
_analysis_inflight: set[int] = set()
_inflight_lock = threading.Lock()


# ── 文案组装 ──


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Agent 报告是 Markdown(## 结论 / ## 判断依据 / …)。逐行取第一个非空行会把
# "## 结论"这个标题本身当成结论，告警消息于是变成"AI 归因：结论"——等于没归因。
_CONCLUSION_KEYWORDS = ("结论", "归因", "根因", "conclusion", "root cause")
_SENTENCE_END = "。！？；：.!?;:"


def _clean_line(raw: str) -> str:
    """剥掉 Markdown 的标题井号、列表符号、引用符与加粗标记。"""
    return raw.strip().lstrip("#>*-• \t").strip().replace("**", "")


def summarize(text: str | None, limit: int = 160) -> str:
    """把 Agent 报告压成一句话，供告警消息 / Webhook / 列表摘要直接使用。

    优先取「结论」小节的正文，没有该小节时退回第一段正文;多行按中文标点习惯
    拼接后再截断，避免把标题、分隔线当成内容。
    """
    if not text:
        return ""
    conclusion: list[str] = []
    fallback: list[str] = []
    in_conclusion = False
    for raw in text.replace("\r", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = _clean_line(stripped).lower()
            in_conclusion = any(word in heading for word in _CONCLUSION_KEYWORDS)
            continue
        body = _clean_line(stripped)
        if not body or set(body) <= set("-=*_~ "):
            continue  # 分隔线之类的装饰行
        if in_conclusion:
            conclusion.append(body)
        elif not fallback:
            fallback.append(body)
    joined = ""
    for part in conclusion or fallback:
        if not joined:
            joined = part
        else:
            joined += part if joined[-1] in _SENTENCE_END else f"；{part}"
        if len(joined) >= limit:
            break
    return joined[:limit] + ("…" if len(joined) > limit else "")


# 归因失败时写进 analysis_text 的前缀;_analysis_note 会剥掉它再取原因，
# 避免消息里出现"AI 归因未得出结果(AI 归因分析失败：…)"这种套娃。
_ANALYSIS_FAILED_PREFIX = "AI 归因分析失败："


def _failure_reason(text: str | None, limit: int = 60) -> str:
    """从 analysis_text 里取出失败原因，让告警消息能说明卡在哪一环。"""
    reason = (text or "").strip()
    if reason.startswith(_ANALYSIS_FAILED_PREFIX):
        reason = reason[len(_ANALYSIS_FAILED_PREFIX) :].strip()
    reason = reason.split("\n")[0].strip()
    return reason[:limit] + ("…" if len(reason) > limit else "")


def _analysis_note(state: str, text: str | None) -> str | None:
    # running 不再进消息:扣住时对外不发，放行后「进行中」话术只会让告警卡
    # 看起来像卡死;归因进度由告警中心的 analysis_state 展示，结论由
    # alert.analysis 卡片补发。
    if state == "running":
        return None
    if state == "completed":
        summary = summarize(text)
        return f"AI 归因：{summary}" if summary else "AI 归因分析已完成"
    if state == "failed":
        reason = _failure_reason(text)
        return (
            f"AI 归因未得出结果({reason})"
            if reason
            else "AI 归因分析未得出结果，请人工排查"
        )
    return None


def compose_message(
    base: str | None,
    remediation_detail: str | None = None,
    analysis_state: str = "",
    analysis_text: str | None = None,
) -> str:
    """基础告警文案 + 处置进度 + 分析结论，拼成对外展示/推送的一条消息。"""
    parts = [
        (base or "").strip(),
        (remediation_detail or "").strip(),
        (_analysis_note(analysis_state, analysis_text) or "").strip(),
    ]
    return "；".join(part for part in parts if part)


def apply_message(event: AlertEvent, base: str) -> None:
    """评估器每轮刷新基础文案时调用:同步 base_message 并保留自愈进度后缀。"""
    record = dict(event.remediation_json or {})
    record["base_message"] = base
    event.remediation_json = record
    event.message = compose_message(
        base, event.remediation_detail, event.analysis_state, event.analysis_text
    )


def apply_recovered(event: AlertEvent) -> None:
    """事件恢复时给处置收尾(在评估事务内调用，随事务一起提交)。"""
    if event.remediation_state not in ("running", "verifying"):
        return
    record = dict(event.remediation_json or {})
    event.remediation_state = "succeeded"
    event.remediation_detail = "自动拉起操作成功，目标已恢复运行"
    record["state"] = "succeeded"
    record["finished_at"] = _iso(_now())
    event.remediation_json = record
    apply_message(event, record.get("base_message") or event.message)


def _update(
    event_id: int,
    *,
    state: str | None = None,
    detail: str | None = None,
    patch: dict | None = None,
    analysis_state: str | None = None,
    analysis_text: str | None = None,
    agent_run_id: int | None = None,
) -> None:
    """把自愈进度写回事件(独立会话;后台线程调用，失败只记日志)。"""
    changed = False
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return
        previous_state = event.remediation_state or ""
        record = dict(event.remediation_json or {})
        if not record.get("base_message"):
            # 首次写入进度前把当前(还没有任何后缀的)消息固化为基础文案，
            # 之后每次重新拼装都基于它，避免"分析进行中…"这类话术层层叠加。
            record["base_message"] = event.message or ""
        if state is not None:
            event.remediation_state = state
            record["state"] = state
        if patch:
            record.update(patch)
        record["updated_at"] = _iso(_now())
        event.remediation_json = record
        if detail is not None:
            event.remediation_detail = detail
        if analysis_state is not None:
            event.analysis_state = analysis_state
        if analysis_text is not None:
            event.analysis_text = analysis_text[:_ANALYSIS_MAX_CHARS]
        if agent_run_id is not None:
            event.agent_run_id = agent_run_id
        event.message = compose_message(
            record.get("base_message") or event.message,
            event.remediation_detail,
            event.analysis_state,
            event.analysis_text,
        )
        db.commit()
        # 只在状态跃迁时通知:同一状态内的文案微调不值得再推一条 Webhook。
        # 处置进度只推**终态**(succeeded/failed/skipped):running/verifying 这类
        # 瞬时中间态几秒内就会被终态覆盖,逐个推卡只会让一次离线告警在两分钟
        # 里连发五六张卡(实测 2026-09-16 用户打回);进度仍可在告警中心的
        # remediation_state 字段实时看到。
        # 归因进度只推终态:running 转换推出去是一张「AI 归因中」的空卡,
        # 结论几秒~几十秒后才是有效内容;未扣住的 container_status 归因
        # 正是走这条补发路径。
        analysis_changed = analysis_state is not None and analysis_state in (
            "completed",
            "failed",
            "skipped",
        )
        remediation_changed = (
            state is not None
            and state != previous_state
            and state in ("succeeded", "failed", "skipped")
        )
        changed = (remediation_changed or analysis_changed) and not record.get(
            "notification_held"
        )
        # 通知被扣住时不推进度:等放行时那条 alert.created 会带上
        # 同样的归因结论，再推一条就是重复消息。
        progress_event = (
            ANALYSIS_WEBHOOK_EVENT if analysis_changed else REMEDIATION_WEBHOOK_EVENT
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to persist auto-heal progress for event %s", event_id)
    finally:
        db.close()
    if changed:
        _notify_progress(event_id, progress_event)


def _notify_progress(
    event_id: int, event_name: str = REMEDIATION_WEBHOOK_EVENT
) -> None:
    """把"正在启动尝试/处置成功/AI 归因完成"这类进度推给订阅方。

    延迟导入 alerts 打破循环依赖;没有 Webhook 订阅时在 _notify 内直接跳过。
    归因结论走 alert.analysis，处置进度走 alert.remediation。
    """
    try:
        from app.services.alerts import notify_event
    except Exception:
        logger.warning("Alert service unavailable, skip %s webhook", event_name)
        return
    notify_event(event_id, event_name, track=False)


# ── 归因结论并入告警通知(扣住 → 放行) ──
#
# 旧时序:告警一触发就发 alert.created，此时归因还没开始，飞书卡片里的「AI 归因」
# 块是空的;约一分钟后归因完成，再单独推一条 alert.remediation。用户收到两条消息，
# 第一条没有结论。
#
# 新时序:指标类告警(CPU/内存/磁盘)先把 alert.created 扣住，等归因落到终态再放行，
# 于是同一条消息的 message 与 analysis.text 里就已经带着结论。
# 扣住期间归因立即开跑，不再等 ALERT_ANALYSIS_SUSTAIN_SECONDS(见 check_analysis):
# 规则默认 sustain_seconds=60 小于它，真等满 180s 的话放行那一刻照样没有结论。
# 放行点覆盖了所有出口(闸门拒绝/启动失败/完成/失败/事件已恢复/超时/进程重启)，
# 保证告警最多迟到 ALERT_ANALYSIS_HOLD_TIMEOUT 秒，绝不会被吞掉。

_release_lock = threading.Lock()
_releasing: set[int] = set()


def hold_created_notification(event_id: int, event_name: str = "alert.created") -> bool:
    """扣住一次告警通知，等 AI 归因结论出来后连同结论一起发。

    返回调用方是否应当跳过本次发送:扣住成功、或该事件本来就还扣着时为 True;
    事件不在 open 状态、或开关关闭时为 False(照旧立即发送)。
    """
    if not ALERT_ANALYSIS_HOLD_NOTIFICATION:
        return False
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None or event.status != "open":
            return False
        record = dict(event.remediation_json or {})
        if record.get("notification_held"):
            # 同一事件本轮又要发一条(冷却到期的重复通知)。已经扣着就同样不能立刻发，
            # 否则会在归因结论之前先把一条没有结论的告警推出去。
            return True
        # 自动归因整个关掉时不必扣住:扣了也立刻被闸门放行，白写两次库。
        # (Agent 未配置这类"跑不起来"的情况交给 check_analysis，同样是立刻放行。)
        if not ALERT_AUTO_ANALYSIS:
            return False
        record["notification_held"] = event_name
        record["notification_held_at"] = _iso(_now())
        event.remediation_json = record
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Failed to hold notification for event %s", event_id)
        return False
    finally:
        db.close()


def release_held_notification(event_id: int, *, reason: str = "") -> bool:
    """放行被扣住的告警通知;幂等——只有确实还扣着才会真的发出去。

    必须在归因结论写回事件之后调用，这样发出去的 payload 才带着结论。
    """
    with _release_lock:
        if event_id in _releasing:
            return False
        _releasing.add(event_id)
    held_name = None
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is not None:
            record = dict(event.remediation_json or {})
            held_name = record.pop("notification_held", None)
            record.pop("notification_held_at", None)
            if held_name:
                event.remediation_json = record
                db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to release notification for event %s", event_id)
        held_name = None
    finally:
        db.close()
        with _release_lock:
            _releasing.discard(event_id)
    if not held_name:
        return False
    logger.info(
        "Releasing held %s for event %s%s",
        held_name,
        event_id,
        f" ({reason})" if reason else "",
    )
    try:
        from app.services.alerts import notify_event
    except Exception:
        logger.warning("Alert service unavailable, cannot release event %s", event_id)
        return False
    notify_event(event_id, held_name, track=True)
    return True


def sweep_expired_notification_holds() -> int:
    """看门狗:把不该再扣着的告警通知放行，返回放行条数。

    三种情况都必须放行，否则告警会被永久吞掉:
      * 事件已不再 open(恢复/关闭)——归因没跑完也要发，且要排在 alert.resolved 前;
      * 归因已落到终态却漏了放行(异常路径);
      * 扣住超时:归因卡死、线程池打满或进程重启导致工作线程没了。
    """
    if not ALERT_ANALYSIS_HOLD_NOTIFICATION:
        return 0
    # 只可能是最近还在被评估的指标告警被扣住，按时间窗过滤避免全表扫描。
    window = _now() - timedelta(seconds=max(ALERT_ANALYSIS_HOLD_TIMEOUT, 60) * 4)
    db = SessionLocal()
    try:
        rows = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.metric.in_(ANALYSIS_METRICS),
                AlertEvent.last_seen_at >= window,
            )
            .all()
        )
        due: list[tuple[int, str]] = []
        for event in rows:
            record = event.remediation_json or {}
            if not record.get("notification_held"):
                continue
            held_at = _parse_iso(record.get("notification_held_at"))
            expired = (
                bool(held_at)
                and (_now() - held_at).total_seconds() > ALERT_ANALYSIS_HOLD_TIMEOUT
            )
            if event.status != "open":
                due.append((event.id, "事件已不再 open"))
            elif event.analysis_state in ("completed", "failed", "skipped"):
                due.append((event.id, f"归因已{event.analysis_state}"))
            elif expired:
                due.append((event.id, f"扣住超过 {ALERT_ANALYSIS_HOLD_TIMEOUT}s"))
            elif held_at is None:
                # 没有时间戳就无法判断超时，宁可放行也不吞告警。
                due.append((event.id, "缺少扣住时间戳"))
    except Exception:
        logger.exception("Failed to sweep held alert notifications")
        return 0
    finally:
        db.close()
    released = 0
    for event_id, reason in due:
        if release_held_notification(event_id, reason=reason):
            released += 1
    return released


# ── 处置目标解析 ──


@dataclass
class Target:
    """一次自动拉起的目标。kind == "none" 时表示无法处置，原因见 skip_reason。"""

    kind: str = "none"  # pve_guest / container / none
    label: str = ""
    connection_id: int | None = None
    node: str | None = None
    guest_type: str | None = None
    vmid: int | None = None
    device_id: int | None = None
    # 容器目标的宿主身份:设备为正 id,虚拟机为合成负数 target_id。
    # _start_target / _container_running 都以它寻址宿主。
    owner_target_id: int | None = None
    container_name: str | None = None
    skip_reason: str | None = None
    # 已确认在运行等"不算失败"的情况，用 verifying 而不是 skipped 收尾
    skip_state: str = "skipped"
    errors: list = field(default_factory=list)


def _skip(reason: str, state: str = "skipped") -> Target:
    return Target(kind="none", skip_reason=reason, skip_state=state)


def _binding_by_ip(db: Session, ip_address: str | None) -> PveGuestBinding | None:
    """离线设备若其实是 PVE 上的虚拟机，按管理 IP 反查运维接入绑定。"""
    if not ip_address:
        return None
    return (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.ip_address == ip_address.strip(),
            PveGuestBinding.enabled == 1,
        )
        .first()
    )


def _resolve_guest(db: Session, event: AlertEvent) -> Target:
    """host_status 告警 → 找到可下发 start 的 PVE QEMU/LXC。"""
    if event.device_id is None:
        decoded = decode_pve_target_id(_as_int(event.resource_id))
        if not decoded:
            return _skip("无法定位该主机对应的虚拟化目标，请人工处理")
        connection_id, vmid = decoded
        device_name = None
    else:
        device = db.query(Device).filter(Device.id == event.device_id).first()
        if device is None:
            return _skip("设备已不存在，无法自动处置")
        device_name = device.name
        binding = _binding_by_ip(db, device.ip_address)
        if binding is None:
            return _skip(
                f"{device.name} 未匹配到 PVE 虚拟机，平台无法远程上电，"
                "请人工检查电源与网络"
            )
        connection_id, vmid = binding.connection_id, binding.vmid

    conn = (
        db.query(PveConnection)
        .filter(PveConnection.id == connection_id, PveConnection.enabled == 1)
        .first()
    )
    if conn is None:
        return _skip("PVE 平台不存在或已停用，无法自动启动")
    try:
        guests = build_client(conn).list_guest_resources(force=True)
    except Exception as exc:
        return _skip(f"连接 PVE 平台失败：{str(exc)[:120]}")

    guest = next((g for g in guests if _as_int(g.get("vmid")) == vmid), None)
    if guest is None:
        return _skip(f"PVE 平台上找不到 vmid={vmid} 的虚拟机，可能已被删除")

    guest_type = str(guest.get("type") or "qemu")
    node = str(guest.get("node") or "")
    name = guest.get("name") or f"VM {vmid}"
    kind_label = "PVE 虚拟机"
    label = f"{kind_label} [{conn.name}] {guest_type.upper()} {vmid} {name}"
    # 历史规则里可能残留模板机目标:模板是克隆源，启动它没有意义
    if is_guest_template(guest):
        return _skip(f"{label} 是模板机(克隆源)，不参与自动启动，请人工确认告警对象")
    if not node:
        return _skip(f"{label} 未返回所在节点，无法下发启动指令")

    if str(guest.get("status") or "") == "running":
        if event.device_id is None:
            # 告警本身就来自 PVE 状态，已在运行说明下一轮会自动恢复
            return _skip(f"{label} 在 PVE 上已处于运行状态，等待状态刷新", "verifying")
        return _skip(
            f"{label} 在 PVE 上已处于运行状态，{device_name} 仍探测离线，"
            "可能是系统内部或网络故障，请人工排查"
        )

    return Target(
        kind="pve_guest",
        label=label,
        connection_id=conn.id,
        node=node,
        guest_type=guest_type,
        vmid=vmid,
    )


def _resolve_container(db: Session, event: AlertEvent) -> Target:
    """container_status 告警 → 找到可执行 docker start 的宿主与容器名。

    宿主身份:设备事件用 ``event.device_id``(正 id);虚拟机事件的 device_id 为 None,
    负数 target_id 在事件创建时存进了处置记录。
    """
    owner_target_id = event.device_id
    if owner_target_id is None:
        owner_target_id = _int_or_none((event.remediation_json or {}).get("target_id"))
    if owner_target_id is None:
        return _skip("容器未归属到可访问的宿主机，无法自动启动")
    if owner_target_id < 0:
        # 虚拟机容器:经 binding 找到宿主,后续用 pve 通道执行 docker start
        binding = _binding_for_target(db, owner_target_id)
        if binding is None:
            return _skip("虚拟机未绑定运维接入(SSH/WinRM)，无法自动启动容器")
        return _resolve_container_on_guest(db, event, binding)
    row = (
        db.query(DeviceContainer)
        .filter(
            DeviceContainer.device_id == owner_target_id,
            DeviceContainer.container_id == event.resource_id,
        )
        .first()
    )
    if row is None and event.resource_name:
        # 快照每周期整批替换，container_id 可能已变化，退回按名称匹配
        row = (
            db.query(DeviceContainer)
            .filter(
                DeviceContainer.device_id == owner_target_id,
                DeviceContainer.name == event.resource_name.rsplit("/", 1)[-1],
            )
            .first()
        )
    if row is None:
        return _skip("容器快照已不存在，可能已被删除或重建")
    try:
        name = validate_container_name(row.name)
    except ValueError:
        return _skip("容器名不合法，已拒绝自动启动")

    target = load_container_target(db, owner_target_id)
    if target is None:
        return _skip("宿主机未配置运维接入(SSH/WinRM)，无法自动启动容器")
    if not target.username or (not target.password and not target.ssh_key):
        return _skip("宿主机未绑定有效凭据，无法自动启动容器")

    device = db.query(Device).filter(Device.id == event.device_id).first()
    host = device.name if device else f"设备 {event.device_id}"
    return Target(
        kind="container",
        label=f"容器 {name}@{host}",
        device_id=owner_target_id,
        owner_target_id=owner_target_id,
        container_name=name,
    )


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _binding_for_target(db: Session, target_id: int) -> PveGuestBinding | None:
    """负数合成 target_id → PVE guest 绑定。"""
    if target_id >= 0:
        return None
    connection_id, vmid = divmod(abs(target_id), 1_000_000)
    return (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.connection_id == connection_id,
            PveGuestBinding.vmid == vmid,
        )
        .first()
    )


def _resolve_container_on_guest(
    db: Session, event: AlertEvent, binding: PveGuestBinding
) -> Target:
    """虚拟机里的容器:快照经 pve_guest_binding_id 归属,执行通道走虚拟机本身。"""
    row = (
        db.query(DeviceContainer)
        .filter(
            DeviceContainer.pve_guest_binding_id == binding.id,
            DeviceContainer.container_id == event.resource_id,
        )
        .first()
    )
    if row is None and event.resource_name:
        row = (
            db.query(DeviceContainer)
            .filter(
                DeviceContainer.pve_guest_binding_id == binding.id,
                DeviceContainer.name == event.resource_name.rsplit("/", 1)[-1],
            )
            .first()
        )
    if row is None:
        return _skip("容器快照已不存在，可能已被删除或重建")
    try:
        name = validate_container_name(row.name)
    except ValueError:
        return _skip("容器名不合法，已拒绝自动启动")
    target = load_container_target(
        db, -(binding.connection_id * 1_000_000 + binding.vmid)
    )
    if target is None:
        return _skip("虚拟机未配置运维接入(SSH/WinRM)，无法自动启动容器")
    if not target.username or (not target.password and not target.ssh_key):
        return _skip("虚拟机未绑定有效凭据，无法自动启动容器")
    conn = (
        db.query(PveConnection)
        .filter(PveConnection.id == binding.connection_id)
        .first()
    )
    host = (
        f"[{conn.name}] {binding.guest_type.upper()} VM {binding.vmid}"
        if conn
        else f"VM {binding.vmid}"
    )
    return Target(
        kind="container",
        label=f"容器 {name}@{host}",
        device_id=None,
        # 宿主身份 = 合成负数 target_id(与事件创建时同一契约)。_start_target 与
        # _container_running 经它走 load_container_target / binding 反查。
        # 不能塞进 device_id:它还会被 _audit_container 写进 devices 外键列
        owner_target_id=-(binding.connection_id * 1_000_000 + binding.vmid),
        container_name=name,
    )


def resolve_target(db: Session, event: AlertEvent) -> Target:
    if event.metric == "container_status":
        return _resolve_container(db, event)
    return _resolve_guest(db, event)


# ── 执行与确认 ──


def _pve_client(db: Session, connection_id: int | None):
    conn = (
        db.query(PveConnection).filter(PveConnection.id == connection_id).first()
        if connection_id
        else None
    )
    return build_client(conn) if conn else None


def _audit_container(target, success: bool, message: str) -> None:
    """自动处置也是一次真实的容器控制，按人工操作同样落审计。"""
    db = SessionLocal()
    try:
        db.add(
            ContainerAction(
                user_id=None,
                device_id=target.device_id,
                device_name=target.label,
                container_name=target.container_name or "",
                action="start",
                success=1 if success else 0,
                message=(message or "")[:300],
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to audit auto container start")
    finally:
        db.close()


def _start_target(target: Target) -> tuple[bool, str]:
    """下发启动指令，返回 (是否成功, 详情/错误)。"""
    if target.kind == "pve_guest":
        db = SessionLocal()
        try:
            client = _pve_client(db, target.connection_id)
            if client is None:
                return False, "PVE 平台不存在或已停用"
            client.power(target.node, target.guest_type, target.vmid, "start")
            return True, "PVE 已受理启动请求"
        except Exception as exc:
            return False, str(exc)[:200]
        finally:
            db.close()

    db = SessionLocal()
    try:
        # 设备容器 device_id 为正;虚拟机容器 device_id=None,宿主身份在
        # owner_target_id(负数)。两者都能被 load_container_target 解析。
        owner = (
            target.device_id if target.device_id is not None else target.owner_target_id
        )
        if owner is None:
            return False, "容器未归属到可访问的宿主机"
        host = load_container_target(db, owner)
        if host is None:
            return False, "宿主机不可用或未配置运维接入"
        code, out, err = run_container_action(host, "start", target.container_name)
        message = (out or err or "").strip()[:200]
        ok = code == 0
        _audit_container(target, ok, message or ("操作成功" if ok else "操作失败"))
        if not ok:
            return False, message or "docker start 执行失败"
        return True, message or "docker start 已执行"
    except Exception as exc:
        return False, str(exc)[:200]
    finally:
        db.close()


def _guest_running(target: Target) -> bool:
    db = SessionLocal()
    try:
        client = _pve_client(db, target.connection_id)
        if client is None:
            return False
        status = client.guest_status(target.node, target.guest_type, target.vmid)
        return str(status.get("status") or "") == "running"
    finally:
        db.close()


def _container_running(target: Target) -> bool:
    from app.services.containers_collector import _collect_one

    # 设备容器 device_id 为正;虚拟机容器 device_id=None、宿主身份是
    # owner_target_id(合成负数,与 _resolve_container 同一约定)。
    owner = target.device_id if target.device_id is not None else target.owner_target_id
    if owner is None:
        return False
    # 全量重采(含 stats):验证只关心容器状态,但持久化是整批重写,
    # 跳过 stats 会把整台机器的容器指标抹成 NULL
    _collect_one(owner)
    db = SessionLocal()
    try:
        query = db.query(DeviceContainer).filter(
            DeviceContainer.name == target.container_name
        )
        if owner > 0:
            query = query.filter(DeviceContainer.device_id == owner)
        else:
            binding = _binding_for_target(db, owner)
            if binding is None:
                return False
            query = query.filter(DeviceContainer.pve_guest_binding_id == binding.id)
        row = query.first()
        return bool(row) and (row.state or "").lower() == "running"
    finally:
        db.close()


def _verify_target(target: Target) -> bool:
    """有界轮询确认目标真的恢复运行(后台线程内执行，允许 sleep)。"""
    if target.kind not in _VERIFY_INTERVAL:
        return False
    probe: Callable[[Target], bool] = (
        _guest_running if target.kind == "pve_guest" else _container_running
    )
    interval = _VERIFY_INTERVAL[target.kind]
    deadline = time.monotonic() + ALERT_REMEDIATION_VERIFY_SECONDS
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            if probe(target):
                return True
        except Exception as exc:
            logger.warning("Auto-heal verify failed for %s: %s", target.label, exc)
    return False


# ── 闸门(手动/自动共用) ──


def check_remediation(event_id: int, *, manual: bool = False) -> str | None:
    """返回不能自动处置的原因;None 表示可以处置。"""
    if not manual and not ALERT_AUTO_REMEDIATION:
        return "自动处置已关闭(ALERT_AUTO_REMEDIATION=false)"
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return "告警事件不存在"
        if event.metric not in REMEDIATION_METRICS:
            return "该告警类型不支持自动启动处置"
        if event.status == "resolved":
            return "告警已恢复，无需处置"
        record = event.remediation_json or {}
        if not manual:
            if record.get("state") == "succeeded":
                return "已完成自动处置"
            attempts = int(record.get("attempts") or 0)
            if attempts >= ALERT_REMEDIATION_MAX_ATTEMPTS:
                return f"已达最大自动尝试次数({ALERT_REMEDIATION_MAX_ATTEMPTS})"
            last = _parse_iso(record.get("last_attempt_at"))
            if last and (_now() - last).total_seconds() < ALERT_REMEDIATION_COOLDOWN:
                return "距上次自动处置不足冷却时间"
        return None
    finally:
        db.close()


def check_analysis(event_id: int, *, manual: bool = False) -> str | None:
    """返回不能做 AI 归因的原因;None 表示可以分析。"""
    if not manual and not ALERT_AUTO_ANALYSIS:
        return "自动 AI 归因已关闭(ALERT_AUTO_ANALYSIS=false)"
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return "告警事件不存在"
        if event.metric not in ANALYSIS_METRICS:
            return "仅 CPU / 内存 / 磁盘 / 容器状态告警支持 AI 归因分析"
        # 归因宿主与处置同口径:device_id(设备) > 处置记录里的 target_id
        # (容器/虚拟机事件写入) > resource_id 里的合成负数(虚拟机指标事件)。
        # 两者都定位不了才拒绝。
        if _analysis_owner(event) is None and not decode_pve_target_id(
            _as_int(event.resource_id)
        ):
            return "该告警未关联具体设备或虚拟机，无法定位主机"
        if event.status == "resolved" and not manual:
            return "告警已恢复，无需分析"
        if not manual:
            if event.analysis_state in ("running", "completed", "failed"):
                return _CONCURRENT_OWNER_REASON
            # 通知被扣住时不再等 sustain 窗口:扣住本身已经是一次防抖(规则自己的
            # sustain_seconds 决定何时开始扣)，再等一轮只会让放行那一刻仍然没有结论，
            # 用户还是先收到一条没有归因的告警、几十秒后再收到第二条。
            if not (event.remediation_json or {}).get("notification_held"):
                triggered = _as_utc(event.first_triggered_at)
                if (
                    triggered
                    and (_now() - triggered).total_seconds()
                    < ALERT_ANALYSIS_SUSTAIN_SECONDS
                ):
                    return "指标未持续超阈值，暂不触发分析"
        if not get_agent_config(db).ready:
            return "Agent 未启用或未配置 LLM(系统管理 → Agent 配置)"
        return None
    finally:
        db.close()


# ── 后台执行 ──


def remediate_event(event_id: int, *, manual: bool = False) -> dict:
    """执行一次自动拉起，并把进度话术写回事件。"""
    reason = check_remediation(event_id, manual=manual)
    if reason:
        logger.info("Skip auto-remediation for event %s: %s", event_id, reason)
        return {"ok": False, "error": reason}

    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return {"ok": False, "error": "告警事件不存在"}
        record = dict(event.remediation_json or {})
        base = record.get("base_message") or event.message or ""
        attempts = int(record.get("attempts") or 0)
        target = resolve_target(db, event)
    finally:
        db.close()

    if target.kind == "none":
        _update(
            event_id,
            state=target.skip_state,
            detail=target.skip_reason,
            patch={"base_message": base, "reason": target.skip_reason},
        )
        return {"ok": False, "error": target.skip_reason}

    attempt = attempts + 1
    _update(
        event_id,
        state="running",
        detail=f"已识别为{target.label}，正在下发启动指令…(第 {attempt}/"
        f"{ALERT_REMEDIATION_MAX_ATTEMPTS} 次)",
        patch={
            "base_message": base,
            "kind": target.kind,
            "action": "start",
            "target": target.label,
            "attempts": attempt,
            "last_attempt_at": _iso(_now()),
            "started_at": record.get("started_at") or _iso(_now()),
        },
    )

    ok, detail = _start_target(target)
    if not ok:
        errors = list(record.get("errors") or []) + [detail]
        exhausted = attempt >= ALERT_REMEDIATION_MAX_ATTEMPTS
        _update(
            event_id,
            state="failed" if exhausted else "running",
            detail=(
                f"自动启动失败：{detail}"
                + (
                    "，已达最大尝试次数，请人工介入"
                    if exhausted
                    else "，将在冷却后重试"
                )
            ),
            patch={"errors": errors[-5:], "error": detail},
        )
        return {"ok": False, "error": detail}

    _update(
        event_id,
        state="verifying",
        detail=f"{detail}，正在确认{target.label}是否恢复运行…",
    )
    if _verify_target(target):
        _update(
            event_id,
            state="succeeded",
            detail="自动拉起操作成功，目标已恢复运行",
            patch={"finished_at": _iso(_now())},
        )
        return {"ok": True}

    exhausted = attempt >= ALERT_REMEDIATION_MAX_ATTEMPTS
    _update(
        event_id,
        state="failed" if exhausted else "verifying",
        detail=(
            f"启动指令已下发，但 {ALERT_REMEDIATION_VERIFY_SECONDS}s 内未确认恢复"
            + ("，请人工介入" if exhausted else "，继续等待采集周期确认")
        ),
        patch={"finished_at": _iso(_now())} if exhausted else None,
    )
    return {"ok": False, "error": "未在限定时间内确认恢复"}


def _analysis_owner(event: AlertEvent) -> int | None:
    """归因宿主身份:device_id(物理设备) > 处置记录 target_id(容器事件创建时写入)。

    负数即 PVE guest 编码;返回 None 表示只能靠 resource_id 兼底解码(旧指标事件)。
    """
    if event.device_id is not None:
        return event.device_id
    return _int_or_none((event.remediation_json or {}).get("target_id"))


def _analysis_target(db: Session, event: AlertEvent):
    """归因目标解析:物理设备直接用 Device;虚拟机事件(device_id=None、
    resource_id 为合成负数 target_id 或处置记录 target_id)与自动化/巡检同口径,
    经 ``_pve_runtime_device`` 转成 AgentTarget(明文凭据/IP/OS，运行时还会
    用 QGA 复核最新 IP)。返回 ``(target, 跳过原因)``。"""
    owner = _analysis_owner(event)
    if owner is not None and owner > 0:
        device = db.query(Device).filter(Device.id == owner).first()
        if device is None:
            return None, "设备已不存在，无法诊断"
        if not device.ip_address:
            return None, "设备未配置 IP 地址，无法诊断"
        return device, None
    if owner is None:
        decoded = decode_pve_target_id(_as_int(event.resource_id))
    else:
        decoded = decode_pve_target_id(owner)
    if not decoded:
        return None, "无法定位该告警对应的虚拟化目标，无法诊断"
    connection_id, vmid = decoded
    binding = (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.connection_id == connection_id,
            PveGuestBinding.vmid == vmid,
            PveGuestBinding.enabled == 1,
        )
        .first()
    )
    if binding is None:
        return None, "虚拟机未配置运维接入绑定，无法诊断"
    # 容器事件的 resource_name 是「宿主/容器」,诊断目标应当是虚拟机本身;
    # 解不出宿主名时用 vmid 占位,避免 AgentTarget.name=None。
    owner_name = (event.resource_name or "").rsplit("/", 1)[0]
    target = SimpleNamespace(
        pve_connection_id=connection_id,
        pve_guest_type=binding.guest_type,
        pve_vmid=vmid,
        device_name=owner_name or f"VM {vmid}",
    )
    runtime = _pve_runtime_device(target)
    if runtime is None:
        return None, "虚拟机未配置有效的运维接入(IP/凭据)，无法诊断"
    return runtime, None


def _analysis_question(event: AlertEvent, device: Device | AgentTarget) -> str:
    from app.services.alerts import METRIC_LABELS

    if event.metric == "container_status":
        container = (event.resource_name or "").rsplit("/", 1)[-1] or "未知容器"
        return (
            f"{device.name}({device.ip_address or '无 IP'})上的容器 {container} "
            "当前状态异常(非 running)。请只读排查:结合容器运行状态、容器内"
            "进程与近期错误日志,判断异常原因(进程崩溃/资源耗尽/配置或依赖错误/"
            "人为停止),并给出可执行的处置建议。"
        )
    label = METRIC_LABELS.get(event.metric, event.metric)
    return (
        f"{device.name}({device.ip_address or '无 IP'})的{label}达到 {event.value:.1f}%，"
        f"已超过告警阈值 {event.threshold:.1f}%。请只读排查:定位当前占用最高的进程/服务，"
        f"判断属于业务增长、异常进程还是资源泄漏，并给出可执行的处置建议。"
    )


def _wait_for_run(run_id: int) -> tuple[str | None, str]:
    """等待 Agent 运行落到终态，返回 (报告, 错误)。

    优先用进程内完成事件唤醒(终态落库即 set,零延迟);事件不存在(旧进程
    创建的 run、或外部脚本直接播的行)时退回秒级轮询。先查再等:诊断经常
    几秒内结束,等一个周期再查会平白多等一轮。
    """
    from app.services.agent import wait_run_completion

    deadline = time.monotonic() + ALERT_ANALYSIS_WAIT_SECONDS
    while True:
        db = SessionLocal()
        try:
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if run is None:
                return None, "诊断记录不存在"
            if run.status == "completed":
                report = (run.report or "").strip()
                return (report, "") if report else (None, "诊断未返回结论")
            if run.status == "failed":
                return None, (run.error or "诊断执行失败")[:300]
        finally:
            db.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, f"诊断超过 {ALERT_ANALYSIS_WAIT_SECONDS}s 未返回结论"
        if not wait_run_completion(run_id, remaining):
            # 事件未注册(非本进程创建):按轮询兑底
            time.sleep(min(_ANALYSIS_POLL_INTERVAL, remaining))


# 「该告警已分析过」拒绝的识别标记:意味着另一个调用拥有这次归因(在跑/已出
# 结论)。此时绝不能在 analyze_event 的 finally 里抢着放行被扣住的通知——
# 归因还没落地,提前放行会发出一张没有结论的卡片,等真正的归因完成后
# alert.analysis 再补发一张,正是要消灭的「两张卡」(2026-09-16 实测踩到:
# 重复提交的第二个调用被闸门拒绝,却把 hold 抢先 pop 掉了)。
_CONCURRENT_OWNER_REASON = "该告警已分析过，如需重跑请在告警中心手动触发"


def _analysis_state(event_id: int) -> str:
    """事件当前归因状态(拒绝路径上判断「归因是否还在跑」用;查不到按空)。"""
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        return (event.analysis_state or "") if event else ""
    finally:
        db.close()


def analyze_event(event_id: int, *, manual: bool = False) -> dict:
    """归因入口:无论走哪条出口，被扣住的告警通知都必须放行。

    放行放在 finally，保证在结论写回事件之后执行——发出去的 payload 因此带着
    analysis.state / analysis.text，飞书卡片的「AI 归因」块在同一条消息里就有内容。
    唯一例外:「已分析过」且归因**仍在跑**(running)——另一个调用拥有这次归因,
    放行权在它手里,这里抢着放行只会把还没带结论的卡片提前发出去(两张卡的
    根因)。而已终态(completed/failed)时没有在跑的归因,通知带着已有结论立即
    放行——冷却到期的重复通知正是这条路径,不能再等看门狗多扣 30~60s。
    """
    result: dict = {}
    try:
        result = _analyze_event_impl(event_id, manual=manual)
        return result
    finally:
        skip = result.get("concurrent") and _analysis_state(event_id) == "running"
        if not skip:
            release_held_notification(event_id)


def _analyze_event_impl(event_id: int, *, manual: bool = False) -> dict:
    """指标过高告警 → 调 Agent 做只读归因，把结论写回事件。"""
    reason = check_analysis(event_id, manual=manual)
    if reason:
        logger.info("Skip alert analysis for event %s: %s", event_id, reason)
        return {
            "ok": False,
            "error": reason,
            # 「已分析过」= 另一个调用拥有这次归因(在跑/已出结论),
            # 被扣住的通知由它收尾放行,本次调用不得提前放行。
            "concurrent": reason == _CONCURRENT_OWNER_REASON,
        }

    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return {"ok": False, "error": "告警事件不存在"}
        device, skip_reason = _analysis_target(db, event)
        if device is None:
            return {"ok": False, "error": skip_reason}
        question = _analysis_question(event, device)
        cfg = get_agent_config(db)
        # 告警归因专用步数预算:交互循环每步=SSH 取证+LLM 往返，是归因超时
        # 的主因;取证项已按指标锁定，压步数不伤结论质量。
        cfg.max_steps = min(
            getattr(cfg, "max_steps", ALERT_ANALYSIS_MAX_STEPS),
            ALERT_ANALYSIS_MAX_STEPS,
        )
        # 按指标锁定取证项，避免固定话术被关键词规则误判、把预算烧在无关的慢命令上。
        run_id = start_diagnosis(
            device,
            question,
            None,
            cfg,
            focus=_ANALYSIS_FOCUS.get(event.metric),
            single_pass=True,
        )
    except Exception as exc:
        logger.exception("Failed to start alert analysis for event %s", event_id)
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        db.close()

    _update(event_id, analysis_state="running", agent_run_id=run_id)
    # _update 会把当前消息固化为基础文案，后续结论只在它之后追加
    report, error = _wait_for_run(run_id)
    if report:
        _update(event_id, analysis_state="completed", analysis_text=report)
        return {"ok": True, "run_id": run_id}
    _update(
        event_id,
        analysis_state="failed",
        analysis_text=f"{_ANALYSIS_FAILED_PREFIX}{error}",
    )
    return {"ok": False, "error": error, "run_id": run_id}


# ── 提交入口(告警评估与 API 共用) ──


def _submit(
    event_id: int,
    worker: Callable[..., dict],
    pool: ThreadPoolExecutor,
    inflight: set[int],
    manual: bool,
) -> bool:
    with _inflight_lock:
        if event_id in inflight:
            return False
        inflight.add(event_id)

    def _run() -> None:
        try:
            worker(event_id, manual=manual)
        except Exception:
            logger.exception("Alert auto-heal worker failed for event %s", event_id)
        finally:
            with _inflight_lock:
                inflight.discard(event_id)

    pool.submit(_run)
    return True


def submit_remediation(event_id: int, *, manual: bool = False) -> bool:
    """非阻塞提交自动拉起;返回是否真的入队(同一事件单飞)。"""
    return _submit(event_id, remediate_event, _heal_pool, _heal_inflight, manual)


def submit_analysis(event_id: int, *, manual: bool = False) -> bool:
    """非阻塞提交 AI 归因分析。"""
    return _submit(event_id, analyze_event, _analysis_pool, _analysis_inflight, manual)


def request_remediation(event_id: int) -> dict:
    """手动触发:先同步过闸门给出明确原因，再交后台执行。"""
    reason = check_remediation(event_id, manual=True)
    if reason:
        return {"ok": False, "error": reason}
    submit_remediation(event_id, manual=True)
    return {"ok": True, "state": "running"}


def request_analysis(event_id: int) -> dict:
    reason = check_analysis(event_id, manual=True)
    if reason:
        return {"ok": False, "error": reason}
    if not submit_analysis(event_id, manual=True):
        # 同一事件已在分析中(单飞)，状态本来就是 running。
        return {"ok": True, "state": "running"}
    # 同步把状态落成 running。否则 analysis_state 要等后台线程排到队才写库，
    # 接口刚返回时前端读到的还是"未分析"，看起来像点了没反应。
    _update(event_id, analysis_state="running")
    return {"ok": True, "state": "running"}


def recover_orphaned_analyses() -> int:
    """服务重启后收敛孤儿归因:被重启打断的退回"未分析"重跑，真失败的保留原因。

    归因在后台线程里跑，进程重启会让 ``recover_interrupted_runs()`` 把 AgentRun
    标成 failed，但 ``alert_events.analysis_state`` 仍停在 ``running``:
    告警中心永远显示"AI 归因中"，而自动链路又因为"该告警已分析过"再也不会重跑。

    按原因分两种收尾:
      * 归因是被重启掐断的(一个诊断步骤都没跑完)——退回"未分析"，下一轮评估自动
        重跑。标成 failed 会让告警消息永久挂着"未得出结果"，而真正出问题的是平台
        自己重启，不是那台服务器，值班人无从"人工排查"。
      * 归因确实跑出了失败结论(LLM 报错/超时/无结论)——保留 failed 并带上原因，
        需要重跑时在告警中心手动触发，避免每轮评估都白烧一次 LLM 调用。

    顺带修一遍历史数据:之前已经被误标成"重启中断"的 open 告警同样退回未分析。
    多实例部署下另一个进程可能确实还在跑，所以只有当对应 AgentRun 不是 running
    时才收敛。
    """
    interrupted_note = f"{_ANALYSIS_FAILED_PREFIX}{INTERRUPTED_RUN_ERROR}"
    db = SessionLocal()
    try:
        stuck = (
            db.query(AlertEvent).filter(AlertEvent.analysis_state == "running").all()
        )
        # 只救还 open 的:已恢复的告警没必要再为它烧一次 LLM 调用。
        mislabeled = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.analysis_state == "failed",
                AlertEvent.status == "open",
                AlertEvent.analysis_text == interrupted_note,
            )
            .all()
        )
        if not stuck and not mislabeled:
            return 0
        recovered = 0
        for event in stuck:
            run = (
                db.query(AgentRun).filter(AgentRun.id == event.agent_run_id).first()
                if event.agent_run_id
                else None
            )
            if run is not None and run.status == "running":
                continue
            if run is None or (run.error or "") == INTERRUPTED_RUN_ERROR:
                _reset_analysis(event)
            else:
                _fail_analysis(event, run.error or "诊断执行失败")
            recovered += 1
        for event in mislabeled:
            _reset_analysis(event)
            recovered += 1
        if recovered:
            db.commit()
            logger.warning(
                "Recovered %d orphaned alert analyses after restart (%d reset for retry)",
                recovered,
                len(mislabeled),
            )
        return recovered
    except Exception:
        db.rollback()
        logger.exception("Failed to recover orphaned alert analyses")
        return 0
    finally:
        db.close()


def _reset_analysis(event: AlertEvent) -> None:
    """退回"未分析":清掉失败话术，下一轮评估会重新提交归因。"""
    record = dict(event.remediation_json or {})
    event.analysis_state = ""
    event.analysis_text = None
    apply_message(event, record.get("base_message") or event.message)


def _fail_analysis(event: AlertEvent, detail: str) -> None:
    record = dict(event.remediation_json or {})
    event.analysis_state = "failed"
    event.analysis_text = f"{_ANALYSIS_FAILED_PREFIX}{detail}"[:_ANALYSIS_MAX_CHARS]
    apply_message(event, record.get("base_message") or event.message)
