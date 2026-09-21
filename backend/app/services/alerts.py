"""Threshold evaluation and Webhook delivery for metric alerts."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import BUSINESS_ALERT_INTERVAL, PVE_STATUS_INTERVAL
from app.database import SessionLocal
from app.models.alert import AlertEvent, AlertRule
from app.models.business import (
    Business,
    BusinessInterface,
    BusinessPveGuest,
    BusinessServer,
)
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.device_container import DeviceContainer, DeviceDockerStatus
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.service_interface import InterfaceProbe, ServiceInterface
from app.models.webhook import Webhook
from app.services import feishu
from app.services.containers_collector import decode_pve_target_id
from app.services.crypto import decrypt
from app.services.feishu import FeishuError
from app.services.monitor import get_latest_statuses
from app.services.pve import build_client, guest_agent_summary
from app.services.pve_guest_status import (
    get_connection_state,
    get_guests_by_connection,
    guest_link_state,
    judged_guest_health,
)
from app.services.remediation import (
    ALERT_AUTO_REMEDIATION,
    ANALYSIS_METRICS,
    ANALYSIS_STATE_LABELS,
    REMEDIATION_METRICS,
    REMEDIATION_STATE_LABELS,
    apply_message,
    apply_recovered,
    hold_created_notification,
    submit_analysis,
    submit_remediation,
    sweep_expired_notification_holds,
)
from app.utils import format_local_time
from app.validators import validate_outbound_url

# 归因类里连 alert.resolved 也不推的指标(2026-09-16 用户定调:触发卡已带
# 归因,恢复卡只是重铺同样的详情+结论)。container_status 例外(2026-09-17
# 用户定调):容器是两步卡片模型,拉起成功后的恢复闭环靠 resolved 卡携带
# 「自动处置」结论——但规则停用/删除的强制收尾仍保持静默(见 _close_event
# 写入的 force_closed 标记与 _notify_event_in_background 的判定)。
_RESOLVED_QUIET_METRICS = ANALYSIS_METRICS - {"container_status"}

logger = logging.getLogger(__name__)
_notification_pool = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="alert-webhook"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_webhook_opener = build_opener(_NoRedirect)

METRIC_LABELS = {
    "cpu_pct": "CPU 使用率",
    "mem_pct": "内存使用率",
    "disk_max_pct": "磁盘使用率",
    "host_status": "主机状态异常",
    "container_status": "容器状态异常",
    "business_status": "业务状态异常",
    "host_online": "主机在线状态",
    "container_running": "容器运行状态",
    "container_restarting": "容器重启状态",
    # business_health / business_server_online / business_interface_up 三个键
    # 从未有评估器发射(死配置,2026-09-17 清理);business_status 是唯一
    # 真实存在的业务指标,evaluate_business_alerts 每轮发射。
}
SEVERITY_LABELS = {"info": "信息", "warning": "警告", "critical": "严重"}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _utc_iso(value: datetime | None) -> str | None:
    """出站 payload 里的时间必须带时区，否则接收方会按自己的本地时间误读。"""
    return None if value is None else _as_utc(value).isoformat()


def _feishu_workflow_body(payload: dict) -> bytes:
    """Minimal Request Body for a Feishu Workflow webhook."""
    alert = payload["alert"]
    device = payload["device"]
    body = {
        "device_name": device.get("name", ""),
        "device_ip": device.get("ip_address") or "",
        "severity": SEVERITY_LABELS.get(
            alert.get("severity"), alert.get("severity", "警告")
        ),
        "alert_time": format_local_time(
            alert.get("last_seen_at") or payload.get("timestamp", "")
        ),
        "alert_content": alert.get("message", ""),
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _webhook_body(webhook: Webhook, payload: dict) -> bytes:
    """Adapt the canonical alert payload to the selected webhook provider."""
    # 飞书工作流 Webhook 接收自定义 Request Body，应直接发送完整的
    # DCN 告警业务 JSON，便于工作流映射到多维表格字段。
    if webhook.provider == "feishu":
        return _feishu_workflow_body(payload)
    # 可选的飞书群自定义机器人格式（不用于多维表格工作流）。
    if webhook.provider == "feishu_bot":
        timestamp = str(int(time.time()))
        secret = decrypt(webhook.secret_enc or "")
        body = {
            "msg_type": "text",
            "content": {
                "text": (
                    f"[{payload['alert']['severity'].upper()}] DCN 告警\n"
                    f"规则：{payload['alert']['rule_name']}\n"
                    f"设备：{payload['device']['name']} ({payload['device'].get('ip_address') or '-'})\n"
                    f"内容：{payload['alert']['message']}\n"
                    f"事件：{payload['event']}"
                )
            },
        }
        if secret:
            signing = f"{timestamp}\n{secret}".encode("utf-8")
            body["timestamp"] = timestamp
            body["sign"] = base64.b64encode(
                hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
            ).decode("utf-8")
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _matches(rule: AlertRule, value: float) -> bool:
    return value > rule.threshold if rule.operator == "gt" else value >= rule.threshold


def _payload(
    event: AlertEvent,
    rule: AlertRule | None,
    device: Device | None,
    event_name: str,
    *,
    guest_ip: str | None = None,
) -> dict:
    # 规则被删除后事件仍保留(rule_id 置空)，推送里用规则名快照兜底。
    # guest_ip:虚拟机事件的运维接入 IP(无本地设备行时地址字段的来源)。
    rule_id = rule.id if rule else event.rule_id
    rule_name = rule.name if rule else (event.rule_name or "已删除的告警规则")
    is_container = (event.resource_type or "") == "container"
    if is_container:
        # 容器告警卡片口径(2026-09-16 用户定调):「对象」只显示容器名,
        # 「地址」显示宿主(服务器/虚拟机)的名称或 IP——resource_name 的
        # 形如「宿主/容器」,正好两者都在。设备宿主的 IP 用 Device 行,
        # 虚机宿主用 guest_ip(运维接入/QGA),都没有时回退宿主名称。
        raw_name = event.resource_name or ""
        container_name = raw_name.rsplit("/", 1)[-1] or raw_name or "未知容器"
        host_label = raw_name.rsplit("/", 1)[0] if "/" in raw_name else ""
        device_name = container_name
        device_ip = (
            (device.ip_address if device else None) or guest_ip or (host_label or None)
        )
        device_type = "container"
    else:
        device_name = device.name if device else (event.resource_name or "")
        device_ip = device.ip_address if device else guest_ip
        device_type = device.type if device else event.resource_type
    payload = {
        "event": event_name,
        "alert": {
            "id": event.id,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "severity": event.severity,
            "status": event.status,
            "metric": event.metric,
            "metric_label": METRIC_LABELS.get(event.metric, event.metric),
            "value": event.value,
            "threshold": event.threshold,
            "message": event.message,
            "first_triggered_at": _utc_iso(event.first_triggered_at),
            "last_seen_at": _utc_iso(event.last_seen_at),
            "resolved_at": _utc_iso(event.resolved_at),
        },
        "device": {
            "id": device.id if device else None,
            "name": device_name,
            "ip_address": device_ip,
            "type": device_type,
        },
        "resource": {
            "type": event.resource_type,
            "id": event.resource_id,
            "name": event.resource_name,
        },
        # 自动处置与 AI 归因的进度:message 已经拼好话术，这里给出结构化字段
        "remediation": {
            "state": event.remediation_state or "",
            "state_label": REMEDIATION_STATE_LABELS.get(
                event.remediation_state or "", "未处置"
            ),
            "detail": event.remediation_detail,
            "action": (event.remediation_json or {}).get("action"),
            "target": (event.remediation_json or {}).get("target"),
            "attempts": (event.remediation_json or {}).get("attempts", 0),
        },
        "analysis": {
            "state": event.analysis_state or "",
            "state_label": ANALYSIS_STATE_LABELS.get(
                event.analysis_state or "", "未分析"
            ),
            "agent_run_id": event.agent_run_id,
            "text": event.analysis_text,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "DCN",
    }
    # Keep flat aliases for Feishu Workflow field mapping, while retaining the
    # structured alert/device objects for integrations that prefer nesting.
    payload.update(
        {
            "alert_id": event.id,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "severity": event.severity,
            "status": event.status,
            "metric": event.metric,
            "metric_label": METRIC_LABELS.get(event.metric, event.metric),
            "value": event.value,
            "threshold": event.threshold,
            "message": event.message,
            "device_id": device.id if device else event.device_id,
            "device_name": device_name,
            "device_ip": device_ip,
            "device_type": device_type,
            "remediation_state": event.remediation_state or "",
            "remediation_detail": event.remediation_detail,
            "analysis_state": event.analysis_state or "",
            "analysis_text": event.analysis_text,
            "agent_run_id": event.agent_run_id,
        }
    )
    return payload


def _deliver_feishu_app(webhook: Webhook, payload: dict) -> dict:
    """飞书自建应用:直接私聊/群发给配置的 receive_id，不经过多维表格工作流。

    url 存的是开放平台域名(默认 https://open.feishu.cn)，app_secret 复用
    secret_enc;接收人与样式放在 webhooks.config 里。
    """
    config = webhook.config or {}
    started = time.monotonic()
    base_result = {"webhook_id": webhook.id, "name": webhook.name}
    try:
        result = feishu.deliver(
            payload,
            base=webhook.url,
            app_id=config.get("app_id"),
            app_secret=decrypt(webhook.secret_enc or ""),
            receivers=config.get("receivers"),
            receive_id_type=config.get("receive_id_type")
            or feishu.AUTO_RECEIVE_ID_TYPE,
            style=config.get("style", "card"),
        )
    except (FeishuError, ValueError) as exc:
        status = exc.status if isinstance(exc, FeishuError) else None
        return {
            **base_result,
            "ok": False,
            "status_code": status,
            "error": str(exc)[:200],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        **base_result,
        "ok": bool(result["ok"]),
        "status_code": result["status_code"],
        "error": result.get("error"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "receivers": result.get("receivers"),
        "delivered": result.get("delivered"),
    }


def _deliver_one(webhook: Webhook, payload: dict) -> dict:
    if webhook.provider == "feishu_app":
        return _deliver_feishu_app(webhook, payload)
    headers = {"Content-Type": "application/json", "User-Agent": "DCN-Alert/1.0"}
    headers.update(webhook.headers or {})
    secret = decrypt(webhook.secret_enc or "")
    if secret:
        headers.setdefault("X-Webhook-Secret", secret)
    request = Request(
        webhook.url,
        data=_webhook_body(webhook, payload),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        validate_outbound_url(webhook.url)
        with _webhook_opener.open(request, timeout=8) as response:
            status = response.status
            return {
                "webhook_id": webhook.id,
                "name": webhook.name,
                "ok": 200 <= status < 300,
                "status_code": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
    except HTTPError as exc:
        return {
            "webhook_id": webhook.id,
            "name": webhook.name,
            "ok": False,
            "status_code": exc.code,
            "error": f"HTTP {exc.code}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "webhook_id": webhook.id,
            "name": webhook.name,
            "ok": False,
            "status_code": None,
            "error": str(exc)[:200],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }


def _event_guest_key(event: AlertEvent) -> tuple[int, int] | None:
    """虚拟机事件 → (connection_id, vmid);物理设备/无法定位返回 None。

    宿主身份:device_id(设备正 id 直接判否) > 处置记录 target_id(容器/虚拟机
    事件写入) > resource_id 负数编码(虚拟机指标事件)。
    """
    owner = event.device_id
    if owner is None:
        owner = _int_or_none((event.remediation_json or {}).get("target_id"))
    if owner is not None and owner > 0:
        return None
    decoded = (
        decode_pve_target_id(owner)
        if owner is not None
        else decode_pve_target_id(_int_or_none(event.resource_id))
    )
    return decoded or None


def _guest_binding_ips(db: Session, events: list[AlertEvent]) -> dict[int, str]:
    """批量解析虚拟机事件的运维接入 IP(绑定保存时 QGA 地址优先,见 routers/pve.py),
    键为 event.id。列表接口一次取回,避免逐行反查绑定。

    告警卡片/告警中心的「地址」字段对虚拟机曾是 '-':事件 device_id 为 NULL,
    链路只查 devices 表。这里按宿主身份反查运维绑定,拿到就显示;无绑定的
    QGA-only 虚机回退「最近已知 IP」(leader 进程的 IP 刷新循环维护,
    列表接口在非 leader 实例上拿不到属预期,与磁盘值缺失同口径)。
    """
    from app.services.pve_guest_status import get_last_known_ip

    wanted: dict[tuple[int, int], set[int]] = {}
    for event in events:
        key = _event_guest_key(event)
        if key is None:
            continue
        wanted.setdefault(key, set()).add(event.id)
    if not wanted:
        return {}
    conn_ids = {key[0] for key in wanted}
    bindings = (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.connection_id.in_(conn_ids),
            PveGuestBinding.enabled == 1,
        )
        .all()
    )
    lookup = {(b.connection_id, b.vmid): (b.ip_address or None) for b in bindings}
    out: dict[int, str] = {}
    for key, event_ids in wanted.items():
        # 绑定 IP(持久) > 最近已知 IP(QGA 运行期问到的,停机后仍可显示)
        ip = lookup.get(key) or get_last_known_ip(key[0], key[1])
        if not ip:
            continue
        for event_id in event_ids:
            out[event_id] = ip
    return out


# QGA 地址查询结果缓存(评估话术与通知共用):QGA ping 每次最多 ~4s,
# 30s 评估周期逐轮重查会把评估拖慢,这里 TTL 5 分钟内复用。
_qga_ip_cache: dict[tuple[int, int], tuple[str | None, float]] = {}
_qga_ip_cache_lock = threading.Lock()
_QGA_IP_TTL = 300.0


def _guest_qga_ip(db: Session, connection_id: int, vmid: int) -> str | None:
    """无运维绑定时兜底:QGA 实时地址(仅通知/详情单条路径,允许秒级开销)。

    QGA-only 虚机(装了 Guest Agent 但没配运维接入)在绑定表里没有行,
    「地址」字段曾是 '-'。这里按快照里的 node/guest_type 调一次
    guest_agent_summary(内部每个 agent 命令 4s 超时),拿 qga_ip_address。
    guest 停机时 QGA 必然无应答(主机状态告警恰恰在这个时点触发),
    此时回退到「最近已知 IP」——guest 运行期间由 IP 刷新循环
    (run_pve_guest_ip_refresh_loop)记下的地址,地址列不至于退回 '-'。
    """
    from app.services.pve_guest_status import (
        get_guest_snapshot,
        get_last_known_ip,
        record_guest_ip,
    )

    conn = (
        db.query(PveConnection)
        .filter(PveConnection.id == connection_id, PveConnection.enabled == 1)
        .first()
    )
    if conn is None:
        return get_last_known_ip(connection_id, vmid)
    now = time.monotonic()
    with _qga_ip_cache_lock:
        cached = _qga_ip_cache.get((connection_id, vmid))
        if cached and now - cached[1] < _QGA_IP_TTL:
            return cached[0] or get_last_known_ip(connection_id, vmid)
    guest = get_guest_snapshot(connection_id, vmid) or {}
    node = guest.get("node")
    gtype = guest.get("guest_type") or "qemu"
    result: str | None = None
    try:
        client = build_client(conn)
        if not node:
            raw = next(
                (
                    g
                    for g in client.list_guest_resources()
                    if int(g.get("vmid", -1)) == vmid
                ),
                None,
            )
            if raw is None:
                result = None
            else:
                node = str(raw.get("node") or "")
                gtype = str(raw.get("type") or "qemu")
        if node:
            summary = guest_agent_summary(client, str(node), gtype, vmid)
            if summary.get("qga_available"):
                result = summary.get("qga_ip_address")
    except Exception:
        logger.debug("QGA fallback ip lookup failed for %s/%s", connection_id, vmid)
    with _qga_ip_cache_lock:
        _qga_ip_cache[(connection_id, vmid)] = (result, time.monotonic())
    if result:
        # 运行中问到的实时地址顺手记入「最近已知」,停机后同一地址继续可显示
        record_guest_ip(connection_id, vmid, result)
        return result
    return get_last_known_ip(connection_id, vmid)


def _guest_display_ip(db: Session, connection_id: int, vmid: int) -> str | None:
    """虚机显示 IP:运维接入绑定优先,QGA 实时地址兜底(带 5 分钟缓存),
    最后回退最近已知 IP(停机期间地址列仍有内容)。
    评估话术与告警地址字段共用同一来源,口径一致。"""
    binding = (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.connection_id == connection_id,
            PveGuestBinding.vmid == vmid,
            PveGuestBinding.enabled == 1,
        )
        .first()
    )
    if binding and binding.ip_address:
        return binding.ip_address
    return _guest_qga_ip(db, connection_id, vmid)


def _guest_binding_ip(db: Session, event: AlertEvent) -> str | None:
    """单个事件的地址:运维接入 IP 优先,QGA 实时地址兜底(单条路径用)。"""
    ip = _guest_binding_ips(db, [event]).get(event.id)
    if ip:
        return ip
    key = _event_guest_key(event)
    if key is None:
        return None
    return _guest_qga_ip(db, key[0], key[1])


def _notify(
    db: Session,
    event: AlertEvent,
    rule: AlertRule | None,
    device: Device | None,
    event_name: str,
    *,
    track: bool = True,
) -> list[dict]:
    """投递一次 Webhook。

    track=False 用于自愈进度这类"附加通知":不覆盖告警本身的通知状态与时间，
    否则一条 alert.remediation 会把 alert.created 的"已发送"冲成"跳过"。
    """
    webhooks = [
        w
        for w in db.query(Webhook).filter(Webhook.enabled.is_(True)).all()
        if event_name in (w.events or [])
        # severity 路由(config.severities,可选项 info/warning/critical):
        # 配了就只投匹配级别的告警,没配(旧钩子/未设置)不过滤——行为不变。
        # 用途:critical 告警走值班群、warning 走普通群。
        if not ((w.config or {}).get("severities"))
        or event.severity in (w.config or {}).get("severities")
    ]
    if not webhooks:
        if track:
            event.notification_status = "skipped"
            event.last_notified_at = datetime.now(timezone.utc)
        return []
    # 虚拟机事件没有本地设备行,地址字段用运维接入 IP(QGA 优先回填过的)
    guest_ip = None if device is not None else _guest_binding_ip(db, event)
    payload = _payload(event, rule, device, event_name, guest_ip=guest_ip)
    results: list[dict] = []
    # 复用模块级线程池,而不是每轮新建——告警频繁时线程创建开销会累积。
    futures = [
        _notification_pool.submit(_deliver_one, hook, payload) for hook in webhooks
    ]
    for future in as_completed(futures):
        results.append(future.result())
    if track:
        event.notification_results = results
        event.notification_status = (
            "sent" if all(item.get("ok") for item in results) else "failed"
        )
        event.last_notified_at = datetime.now(timezone.utc)
    return results


# ── 聚合降噪:同一评估轮内同规则的多条通知合并成一张卡 ──
#
# 场景:交换机/宿主一挂,它名下几十台设备同时离线 → 旧逻辑每台一张卡瞬间
# 轰炸。现在同一轮评估里同 (rule, event_name) 的多条通知合并成一张「N 个对象」
# 的卡。跨轮不聚合(陆续发生的告警本来就该逐条提醒)。归因类(cpu/mem/disk/
# container)不聚合——单卡片模型要求每张卡带自己的归因结论,合并没法带。


def _aggregatable(events: list[AlertEvent]) -> bool:
    return bool(events) and all(e.metric not in ANALYSIS_METRICS for e in events)


def _aggregate_payload(
    events: list[AlertEvent], rule: AlertRule | None, event_name: str
) -> dict:
    """合并卡 payload:对象列表 + 汇总话术;字段结构与单事件卡兼容(build_card 复用)。"""
    sample = events[0]
    rule_id = rule.id if rule else sample.rule_id
    rule_name = rule.name if rule else (sample.rule_name or "已删除的告警规则")
    names = [
        e.resource_name or (e.device_id and str(e.device_id)) or "?" for e in events
    ]
    # 消息:前 5 个对象逐行列出,其余折叠计数
    head = "\n".join(
        f"· {name}：{(e.message or '').split('；')[0].split('\n')[0]}"
        for name, e in list(zip(names, events, strict=False))[:5]
    )
    more = f"\n… 以及另外 {len(events) - 5} 个对象" if len(events) > 5 else ""
    summary = f"同一条规则在 {len(events)} 个对象上触发：\n{head}{more}"
    # 聚合后无法逐对象展开卡片,详情块直接给汇总列表
    payload = {
        "event": event_name,
        "alert": {
            "id": sample.id,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "severity": max(
                (e.severity for e in events),
                key=lambda s: {"info": 0, "warning": 1, "critical": 2}.get(s, 1),
            ),
            "status": sample.status,
            "metric": sample.metric,
            "metric_label": METRIC_LABELS.get(sample.metric, sample.metric),
            "value": None,
            "threshold": sample.threshold,
            "message": summary,
            "first_triggered_at": _utc_iso(
                min(_as_utc(e.first_triggered_at) for e in events)
            ),
            "last_seen_at": _utc_iso(max(_as_utc(e.last_seen_at) for e in events)),
            "resolved_at": _utc_iso(
                max(_as_utc(e.resolved_at or e.last_seen_at) for e in events)
            ),
        },
        "device": {
            "id": None,
            "name": f"{len(events)} 个对象",
            "ip_address": None,
            "type": "aggregate",
        },
        "resource": {
            "type": "aggregate",
            "id": None,
            "name": f"{rule_name} × {len(events)}",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "DCN",
    }
    payload.update(
        {
            "rule_name": rule_name,
            "severity": payload["alert"]["severity"],
            "message": summary,
            "device_name": f"{len(events)} 个对象",
        }
    )
    return payload


def _notify_aggregate(
    db: Session,
    events: list[AlertEvent],
    rule: AlertRule | None,
    event_name: str,
) -> list[dict]:
    """聚合投递:合并卡发一次,组内事件逐条标记通知状态(与单事件 _notify 同口径)。"""
    # 跨实例去重:组内任一事件被占住(另一实例刚发过同一张卡)整组放弃,
    # 避免双实例各聚一张相同卡(与单事件 _claim_notification 同一竞态)。
    claimed = all(_claim_notification(db, e.id) for e in events)
    if not claimed:
        return []
    payload = _aggregate_payload(events, rule, event_name)
    hooks = _hooks_for(db, event_name, payload["alert"]["severity"])
    if not hooks:
        for event in events:
            event.notification_status = "skipped"
        return []
    futures = [_notification_pool.submit(_deliver_one, hook, payload) for hook in hooks]
    results = [future.result() for future in as_completed(futures)]
    ok = all(item.get("ok") for item in results)
    now = datetime.now(timezone.utc)
    for event in events:
        event.notification_status = "sent" if ok else "failed"
        event.last_notified_at = now
        event.notification_results = results
    return results


def _hooks_for(db: Session, event_name: str, severity: str) -> list[Webhook]:
    """按事件名 + 级别路由筛选启用的钩子(与 _notify 同口径的独立实现)。"""
    return [
        w
        for w in db.query(Webhook).filter(Webhook.enabled.is_(True)).all()
        if event_name in (w.events or [])
        and (
            not (w.config or {}).get("severities")
            or severity in (w.config or {}).get("severities")
        )
    ]


def _dispatch_notifications(db: Session, notifications: list[tuple[int, str]]) -> None:
    """评估器统一的发送口:可聚合的组发合并卡,其余逐条发。

    单条通知保持旧行为(经 notify_event 后台队列,静默窗口在它的出口里拦);
    聚合组在这里同步投递。两条硬约束:
    * 静默窗口必须对聚合卡同样生效——否则维护期多条同时触发的告警会聚成
      一张卡穿透静默(单条被拦、聚合版反而发出,比不聚合更错);
    * 聚合标记(sent/failed/skipped)必须落库——评估器在 commit 后才调本函数,
      末尾不补 commit 的话标记会被会话关闭回滚:事件永远挂着「待发送」,
      冷却重发条件(status != pending)也永不满足,等于意外永久静音。
    """
    if not notifications:
        return
    event_ids = [event_id for event_id, _name in notifications]
    rows = {
        row.id: row
        for row in db.query(AlertEvent).filter(AlertEvent.id.in_(event_ids)).all()
    }
    rule_ids = {row.rule_id for row in rows.values() if row.rule_id is not None}
    rules = (
        {
            row.id: row
            for row in db.query(AlertRule).filter(AlertRule.id.in_(rule_ids)).all()
        }
        if rule_ids
        else {}
    )
    # 同 (event_name, rule_id) 分组;无规则(已删)的按单事件发(聚合对已删规则意义低)
    groups: dict[tuple[str, int], list[AlertEvent]] = {}
    singles: list[tuple[int, str]] = []
    for event_id, event_name in notifications:
        row = rows.get(event_id)
        if row is None or row.rule_id is None:
            singles.append((event_id, event_name))
            continue
        # 静默窗口:命中即拦(与单事件出口同口径——标 skipped+计数,连 resolved
        # 一起静默),聚合组里被拦掉的事件不再参与分组
        window = _active_mute_window(db, row)
        if window is not None:
            row.notification_status = "skipped"
            _count_muted(window.id)
            continue
        groups.setdefault((event_name, row.rule_id), []).append(row)
    for (event_name, rule_id), events in groups.items():
        if len(events) > 1 and _aggregatable(events):
            _notify_aggregate(db, events, rules.get(rule_id), event_name)
        else:
            for event in events:
                singles.append((event.id, event_name))
    for event_id, event_name in singles:
        notify_event(event_id, event_name)
    # 聚合路径改写过事件行(标记/静默 skipped),必须提交;单事件路径的后台
    # 线程在独立会话里自行提交,不受影响。提交失败不回滚投递(卡已发出),只记日志。
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to persist aggregate notification marks")


def _hold_analysis_notifications(
    notifications: list[tuple[int, str]], analysis_candidates: set[int]
) -> set[tuple[int, str]]:
    """把本轮要发的指标告警 alert.created 扣住，等 AI 归因结论一起发。

    返回真正被扣住的 ``(event_id, event_name)``;没扣住的照旧立即发送。
    扣住后由 ``analyze_event`` 收尾放行，``sweep_expired_notification_holds``
    作为看门狗兜底，所以告警最多迟到一个 HOLD_TIMEOUT，不会被吞掉。
    """
    held: set[tuple[int, str]] = set()
    for event_id, event_name in notifications:
        if event_name != "alert.created" or event_id not in analysis_candidates:
            continue
        if hold_created_notification(event_id, event_name):
            held.add((event_id, event_name))
    return held


def _pve_metric_entries(db: Session, rules: list[AlertRule]) -> dict[int, dict]:
    """性能指标评估用的虚拟机条目。

    cpu_pct/mem_pct 来自 pve_guest_status 快照(PVE API,30s 一轮);
    disk_max_pct 来自 guest_fs_metrics 的低频采集缓存(QGA get-fsinfo 优先,
    运维接入 SSH/WinRM 兑底,默认 300s 一轮——磁盘值本身变得慢,QGA/SSH
    又比读快照贵得多)。快照非 running 或平台不可达时 available=False,与
    设备侧"离线不评估"口径一致;磁盘缓存缺失时该指标为 None,评估侧跳过
    (不误关既有事件)。
    """
    wanted = any(
        r.metric in ("cpu_pct", "mem_pct", "disk_max_pct")
        or any((tid or 0) < 0 for tid in (r.target_device_ids or []))
        for r in rules
    )
    if not wanted:
        return {}
    from app.services.guest_fs_metrics import get_guest_disk_entry

    out: dict[int, dict] = {}
    for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
        state = get_connection_state(conn.id)
        reachable = bool(state and state.get("reachable"))
        guests = get_guests_by_connection(conn.id) if reachable else {}
        checked = state.get("checked_at") if state else None
        sampled = datetime.fromtimestamp(checked, tz=timezone.utc) if checked else None
        for vmid, guest in guests.items():
            running = str(guest.get("status") or "") == "running"
            maxmem = int(guest.get("maxmem") or 0)
            mem = int(guest.get("mem") or 0)
            entry = {
                "available": reachable and running,
                "sampled_at": sampled,
                "cpu_pct": round(float(guest.get("cpu") or 0.0) * 100.0, 1)
                if running
                else None,
                "mem_pct": round(mem / maxmem * 100.0, 1)
                if running and maxmem
                else None,
            }
            disk = get_guest_disk_entry(conn.id, vmid)
            if disk:
                # 磁盘值用采集时刻而非评估时刻(sustain 回溯校准口径)
                entry["disk_max_pct"] = disk.get("disk_max_pct")
                entry["disk_sampled_at"] = (
                    datetime.fromtimestamp(disk["sampled_at"], tz=timezone.utc)
                    if disk.get("sampled_at")
                    else None
                )
            out[-(conn.id * 1_000_000 + vmid)] = entry
        # 平台不可达/无快照时,已知绑定过的虚拟机也占位(available=False),
        # 让既有事件走"指标消失"路径而不是永远悬空
        if not guests:
            for binding in (
                db.query(PveGuestBinding)
                .filter(
                    PveGuestBinding.connection_id == conn.id,
                    PveGuestBinding.enabled == 1,
                )
                .all()
            ):
                out.setdefault(
                    -(conn.id * 1_000_000 + binding.vmid), {"available": False}
                )
    return out


def _pve_guest_names(db: Session) -> dict[int, str]:
    """负数 target_id → 展示名([连接名] 虚拟机名),事件/消息里用。"""
    names: dict[int, str] = {}
    for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
        for vmid, guest in get_guests_by_connection(conn.id).items():
            names[-(conn.id * 1_000_000 + vmid)] = (
                f"[{conn.name}] {guest.get('name') or f'VM {vmid}'}"
            )
    return names


def _sampled_at(entry: dict, metric: str, now: datetime) -> datetime:
    """条目携带的样本时间(回溯用);缺失时回退评估时刻。

    虚拟机磁盘指标用 fs 采集时刻(``disk_sampled_at``)——它的采集周期(默认
    300s)与 PVE 快照(30s)不同,拿快照时刻当磁盘样本时刻会让 sustain 起算
    平白超前近 5 分钟。
    """
    ts = entry.get("disk_sampled_at") if metric == "disk_max_pct" else None
    if ts is None:
        ts = entry.get("sampled_at")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return min(ts, now)
    return now


_evaluate_lock = threading.Lock()


def evaluate_alerts(entries: dict[int, dict]) -> None:
    """唯一入口:指标周期(60s)与 PVE 快照周期(30s)都会调，加锁串行，
    避免两轮并发对同一 (rule, target) 重复建事件。
    """
    with _evaluate_lock:
        _evaluate_alerts_unlocked(entries)


def _evaluate_alerts_unlocked(entries: dict[int, dict]) -> None:
    """Evaluate the latest metrics and create/update/resolve alert events.

    设备条目来自 metrics_collector(SSH/WinRM 采集);PVE 虚拟机条目由
    _pve_metric_entries 从 pve_guest_status 快照(PVE API, 30s 一轮)合成,
    负数 target_id 与 host_status/容器同一契约。磁盘使用率只有设备侧有,
    虚拟机条目不带 disk_max_pct。
    """
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
        if not rules:
            return
        # 性能指标目标:正 id=设备,负 id=虚拟机(全部对象时两类都查)
        _merge = dict(entries)
        _merge.update(_pve_metric_entries(db, rules))
        entries = _merge
        guest_names = _pve_guest_names(db)
        devices = {
            d.id: d
            for d in db.query(Device)
            .filter(
                Device.id.in_([k for k in entries if k > 0]),
                Device.type.in_(OPS_TARGET_TYPES),
            )
            .all()
        }
        existing = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.rule_id.in_([rule.id for rule in rules]),
                AlertEvent.status.in_(("pending", "open")),
                # 虚拟机事件 device_id=None,宿主身份在 resource_id(负数)
                or_(
                    AlertEvent.device_id.in_([k for k in entries if k > 0]),
                    AlertEvent.device_id.is_(None),
                ),
            )
            .all()
        )
        current_map = {
            (
                event.rule_id,
                event.resource_type,
                event.resource_id or str(event.device_id),
            ): event
            for event in existing
        }
        notifications: list[tuple[int, str]] = []
        analysis_candidates: set[int] = set()
        for rule in rules:
            if rule.metric not in ("cpu_pct", "mem_pct", "disk_max_pct"):
                continue
            target_ids = set(rule.target_device_ids or entries.keys())
            for device_id in target_ids:
                is_guest = device_id < 0
                device = None if is_guest else devices.get(device_id)
                metric_data = entries.get(device_id, {})
                if is_guest:
                    if not metric_data.get("available"):
                        continue
                elif not device or not metric_data.get("available"):
                    continue
                raw_value = metric_data.get(rule.metric)
                if raw_value is None:
                    continue
                value = float(raw_value)
                display_name = (
                    guest_names.get(device_id)
                    if is_guest
                    else (device.name if device else str(device_id))
                )
                current = current_map.get((rule.id, "device", str(device_id)))
                if _matches(rule, value):
                    message = f"{display_name} {METRIC_LABELS.get(rule.metric, rule.metric)} {value:.1f}% 已超过阈值 {rule.threshold:.1f}%"
                    if current and current.status == "pending":
                        current.value = value
                        current.last_seen_at = now
                        current.occurrence_count += 1
                        apply_message(current, message)
                        if (
                            now - _as_utc(current.first_triggered_at)
                        ).total_seconds() >= rule.sustain_seconds:
                            current.status = "open"
                            current.notification_status = "pending"
                            notifications.append((current.id, "alert.created"))
                    elif current:
                        current.value = value
                        current.last_seen_at = now
                        current.occurrence_count += 1
                        apply_message(current, message)
                        # 已知晓(snooze)的事件冷却到期不再重发:评估/归因照常,
                        # 恢复时照常推 alert.resolved(恢复分支不受此影响)
                        if (
                            current.notification_status != "pending"
                            and not current.snoozed
                            and (
                                not current.last_notified_at
                                or (
                                    now - _as_utc(current.last_notified_at)
                                ).total_seconds()
                                >= rule.cooldown_seconds
                            )
                        ):
                            current.notification_status = "pending"
                            notifications.append((current.id, "alert.created"))
                    else:
                        current = AlertEvent(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            # 虚拟机事件 device_id=None,宿主身份(负数 target_id)
                            # 落在 resource_id——与 host_status/容器事件同一约定,
                            # 失效清理/范围收敛按 resource_id 反查。
                            device_id=None if is_guest else device_id,
                            # 宿主身份冗余列:ACL 查询走索引(见模型注释)
                            owner_target_id=device_id,
                            resource_type="device",
                            resource_id=str(device_id),
                            resource_name=display_name,
                            metric=rule.metric,
                            value=value,
                            threshold=rule.threshold,
                            severity=rule.severity,
                            status="open" if rule.sustain_seconds <= 0 else "pending",
                            message=message,
                            # 回溯到样本时间:sustain 从指标真实越限时刻起算，
                            # 而不是「被评估轮看到」的时刻，消掉最多一个采集周期的误差。
                            first_triggered_at=_sampled_at(
                                metric_data, rule.metric, now
                            ),
                            last_seen_at=now,
                            occurrence_count=1,
                            notification_status="pending",
                        )
                        db.add(current)
                        db.flush()
                        current_map[(rule.id, "device", str(device_id))] = current
                        if current.status == "open":
                            notifications.append((current.id, "alert.created"))
                elif current:
                    if current.status == "pending":
                        db.delete(current)
                    else:
                        apply_recovered(current)
                        current.status = "resolved"
                        current.resolved_at = now
                        current.last_seen_at = now
                        current.notification_status = "pending"
                        notifications.append((current.id, "alert.resolved"))
        # 指标持续过高 → 交给 Agent 只读归因(是否真的执行由 remediation 内部闸门决定)
        for event in current_map.values():
            if event.status == "open" and event.metric in ANALYSIS_METRICS:
                analysis_candidates.add(event.id)
        db.commit()
        # 看门狗先跑:上一轮扣住的通知若已出结论/已恢复/超时，先放行，
        # 保证 alert.created 排在同一轮的 alert.resolved 之前。
        sweep_expired_notification_holds()
        held = _hold_analysis_notifications(notifications, analysis_candidates)
        unheld = [
            (event_id, event_name)
            for event_id, event_name in notifications
            if (event_id, event_name) not in held
        ]
        # 聚合发送:同轮同规则多条通知合并(归因类除外,见 _dispatch_notifications)
        _dispatch_notifications(db, unheld)
        for event_id in analysis_candidates:
            submit_analysis(event_id)
    except Exception:
        db.rollback()
        logger.exception("Alert evaluation failed")
    finally:
        db.close()


def _evaluate_resource_rules(resources: list[dict]) -> None:
    """Evaluate state-based rules. Each resource dict has type/id/name/device/value/message."""
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
        relevant = [r for r in rules if any(x["metric"] == r.metric for x in resources)]
        if not relevant:
            return
        # 只取本轮 relevant 规则的事件:其它指标规则(cpu/mem/disk 等)的事件这轮
        # 既不会比对也不会收尾,全量加载只是白搬(旧行为还会让 host_status 事件
        # 在容器评估周期里被重复提交处置闸门,靠闸门幂等兜着)。
        existing = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.rule_id.in_([rule.id for rule in relevant]),
                AlertEvent.status.in_(("pending", "open")),
            )
            .all()
        )
        current_map = {
            (e.rule_id, e.resource_type, e.resource_id or str(e.device_id)): e
            for e in existing
        }
        notifications = []
        remediation_candidates: set[int] = set()
        analysis_candidates: set[int] = set()
        analysis_hold_ids: set[int] = set()
        for item in resources:
            for rule in relevant:
                if item["metric"] != rule.metric:
                    continue
                targets = rule.target_device_ids or []
                if (
                    rule.metric.startswith("host_")
                    and targets
                    and item.get("target_id", item.get("device_id")) not in targets
                ):
                    continue
                if rule.metric.startswith("container_"):
                    container_targets = {
                        str(v) for v in (rule.target_container_ids or [])
                    }
                    # 归属按 target_id 比较:普通设备为正 id,虚拟机为合成负数 id
                    # (与主机状态规则/角色管理同一契约),因此「勾虚拟机 = 其全部容器」。
                    if (
                        (targets or container_targets)
                        and item.get("target_id") not in targets
                        and str(item["id"]) not in container_targets
                    ):
                        continue
                if (
                    rule.metric.startswith("business_")
                    and rule.target_business_ids
                    and item.get("business_id") not in rule.target_business_ids
                ):
                    continue
                key = (rule.id, item["type"], str(item["id"]))
                current = current_map.get(key)
                matched = _matches(rule, float(item["value"]))
                if matched:
                    message = item["message"]
                    # 两步卡片第一步:可拉起对象异常立刻发卡,详情写明正在
                    # 触发自动拉起;成功后由终态推第二条。**物理设备**的
                    # host_status 不拼此话术(也无法拉起):机房设备无法远程上电,
                    # 拼了只会误导(虚拟机=resource 构造时 device_id=None;
                    # 设备行 device_id 非空)。容器不受影响:设备上的容器走
                    # SSH/WinRM 拉起,照常拼。
                    if (
                        rule.metric in REMEDIATION_METRICS
                        and ALERT_AUTO_REMEDIATION
                        and (
                            rule.metric != "host_status"
                            or item.get("device_id") is None
                        )
                    ):
                        message = f"{message}；正在触发自动拉起操作"
                    if current is None:
                        current = AlertEvent(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            device_id=item.get("device_id"),
                            # 宿主身份冗余列(与 remediation_json.target_id 同源):
                            # ACL 查询走索引,业务事件无宿主为 None
                            owner_target_id=item.get("target_id"),
                            resource_type=item["type"],
                            resource_id=str(item["id"]),
                            resource_name=item.get("name"),
                            metric=rule.metric,
                            value=float(item["value"]),
                            threshold=rule.threshold,
                            severity=rule.severity,
                            status="open" if rule.sustain_seconds <= 0 else "pending",
                            message=message,
                            # 回溯到样本时刻(虚拟机 host_status 带 PVE 快照
                            # checked_at):sustain 从真实状态变化时刻起算
                            first_triggered_at=(
                                item.get("sampled_at")
                                if isinstance(item.get("sampled_at"), datetime)
                                else now
                            ),
                            last_seen_at=now,
                            occurrence_count=1,
                            notification_status="pending",
                            # 容器事件的 device_id 对虚拟机为 None,宿主身份(负数
                            # target_id)存进处置记录,供范围收敛/失效清理反查。
                            remediation_json=(
                                {"target_id": item["target_id"]}
                                if item.get("target_id") is not None
                                else None
                            ),
                        )
                        db.add(current)
                        db.flush()
                        current_map[key] = current
                        if current.status == "open":
                            notifications.append((current.id, "alert.created"))
                    else:
                        current.value = float(item["value"])
                        apply_message(current, message)
                        current.last_seen_at = now
                        current.occurrence_count += 1
                        if (
                            current.status == "pending"
                            and (
                                now - _as_utc(current.first_triggered_at)
                            ).total_seconds()
                            >= rule.sustain_seconds
                        ):
                            current.status = "open"
                            current.notification_status = "pending"
                            notifications.append((current.id, "alert.created"))
                        elif (
                            current.status == "open"
                            and not current.snoozed
                            and (
                                not current.last_notified_at
                                or (
                                    now - _as_utc(current.last_notified_at)
                                ).total_seconds()
                                >= rule.cooldown_seconds
                            )
                        ):
                            current.notification_status = "pending"
                            notifications.append((current.id, "alert.created"))
                elif current:
                    if current.status == "pending":
                        db.delete(current)
                    elif current.status == "open":
                        apply_recovered(current)
                        current.status = "resolved"
                        current.resolved_at = now
                        current.last_seen_at = now
                        current.notification_status = "pending"
                        notifications.append((current.id, "alert.resolved"))
        # 仍然离线/异常的虚拟机与容器 → 尝试自动拉起(闸门在 remediation 内部)
        # 容器状态告警同样 AI 归因(与指标告警同一单卡片模型):扣住 alert.created
        # 等结论一起发。分析候选只收**本轮真正通知过**的事件——current_map 查的
        # 是全表 pending/open,不收窄的话 mem_pct 等指标事件会在这里被每 30s
        # 重复提交一次(它们归指标评估路径管),多一条并发调用源,曾把还没带
        # 结论的扣住通知提前放行(两张卡的根因之一)。
        notified_created = {
            event_id for event_id, name in notifications if name == "alert.created"
        }
        # host_status/container_status 是**两步卡片**模型(2026-09-16 用户定调):
        # 离线/异常**立刻**发卡(详情已带「正在触发自动拉起操作」,见 matched
        # 分支的话术追加),拉起成功后由终态推第二条;不扣住通知。归因扣住仅剩
        # cpu/mem/disk——container_status 的归因结论改走 alert.analysis 补发。
        # 自动拉起仅对可拉起对象:虚拟机 host_status(device_id=None,身份在
        # resource_id 负数)与容器(设备容器走 SSH 拉起);物理设备无法远程上电,
        # 不进拉起流程(2026-09-16 用户定调,手动「尝试拉起」按钮仍可用并给
        # 明确 skip 提示)。
        for event in current_map.values():
            if (
                event.status == "open"
                and event.metric in REMEDIATION_METRICS
                and (event.metric != "host_status" or event.device_id is None)
            ):
                remediation_candidates.add(event.id)
            if (
                event.status == "open"
                and event.metric in ANALYSIS_METRICS
                and event.id in notified_created
            ):
                analysis_candidates.add(event.id)
                if event.metric != "container_status":
                    analysis_hold_ids.add(event.id)
        db.commit()
        # 看门狗先跑:上一轮扣住的通知若已出结论/已恢复/超时,先放行,
        # 保证 alert.created 排在同一轮的 alert.resolved 之前。
        sweep_expired_notification_holds()
        held = _hold_analysis_notifications(notifications, analysis_hold_ids)
        unheld = [
            (event_id, name)
            for event_id, name in notifications
            if (event_id, name) not in held
        ]
        # 聚合发送:同轮同规则多条通知合并(归因类除外,见 _dispatch_notifications)。
        # 典型场景:宿主/交换机一挂,它名下 N 台设备同时离线 → 一张合并卡。
        _dispatch_notifications(db, unheld)
        for event_id in remediation_candidates:
            submit_remediation(event_id)
        for event_id in analysis_candidates:
            submit_analysis(event_id)
    except Exception:
        db.rollback()
        logger.exception("State alert evaluation failed")
    finally:
        db.close()


def evaluate_host_status_alerts() -> None:
    statuses = get_latest_statuses()
    if not statuses:
        return
    db = SessionLocal()
    try:
        devices = {
            d.id: d
            for d in db.query(Device)
            .filter(Device.id.in_(statuses.keys()), Device.type.in_(OPS_TARGET_TYPES))
            .all()
        }
    finally:
        db.close()
    resources = [
        {
            "type": "device",
            "id": did,
            "target_id": did,
            "device_id": did,
            "name": devices[did].name,
            "metric": "host_status",
            "value": 1 if status == "offline" else 0,
            "message": (
                f"主机 {devices[did].name}({devices[did].ip_address}) 当前已离线或无法连接"
                if devices[did].ip_address
                else f"主机 {devices[did].name} 当前已离线或无法连接"
            ),
        }
        for did, status in statuses.items()
        if did in devices
    ]
    # PVE 虚拟机的 host_status 已改由 30s 快照循环评估(见
    # evaluate_pve_guest_host_status):60s 指标周期 + 每轮对每个平台实时调
    # PVE API 的旧路径,让虚机离线告警迟到 2~3 个周期,与规则预设的
    # sustain 严重不符。
    _evaluate_resource_rules(resources)


def evaluate_pve_guest_host_status() -> None:
    """从 pve_guest_status 快照评估虚拟机主机状态(30s 周期,零 PVE API 成本)。

    快照循环本来就每 30s 拉全量 guest 资源,评估直接读内存快照;PVE 不可达时
    跳过该平台(与旧路径的 API 异常 continue 同语义:拿不到可信清单就保持
    既有事件,不误触发也不误收敛)。``first_triggered_at`` 回溯快照 checked_at,
    sustain 从真实状态变化时刻起算,消掉一个评估周期的误差。
    """
    db = SessionLocal()
    try:
        resources = []
        for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
            state = get_connection_state(conn.id)
            reachable = bool(state and state.get("reachable"))
            if not reachable:
                continue
            checked = state.get("checked_at") if state else None
            sampled = (
                datetime.fromtimestamp(checked, tz=timezone.utc) if checked else None
            )
            for vmid, guest in get_guests_by_connection(conn.id).items():
                running = str(guest.get("status") or "") == "running"
                target_id = -(conn.id * 1_000_000 + vmid)
                name = guest.get("name") or f"VM {vmid}"
                # 详情话术带 IP(运维接入优先,QGA 兜底且带缓存)——告警对象
                # 只是非 running 的虚机,离线集合通常很小,不拖慢评估。
                ip = None if running else _guest_display_ip(db, conn.id, vmid)
                resources.append(
                    {
                        "type": "device",
                        "id": target_id,
                        "target_id": target_id,
                        "device_id": None,
                        "name": f"{conn.name}/{name}",
                        "metric": "host_status",
                        "value": 0 if running else 1,
                        "message": (
                            f"虚拟机 {name}({ip}) 当前离线或未运行"
                            if ip
                            else f"虚拟机 {name} 当前离线或未运行"
                        ),
                        "sampled_at": sampled,
                    }
                )
    finally:
        db.close()
    _evaluate_resource_rules(resources)


def _container_resource(
    c: DeviceContainer, *, target_id: int, device_id: int | None, host_label: str
) -> dict:
    """单个容器快照 → 资源评估项。target_id 为宿主身份(设备正 id / 虚拟机合成负数 id)。"""
    state = (c.state or "").lower()
    restarting = state == "restarting" or (c.status or "").lower().startswith(
        "restarting"
    )
    return {
        "type": "container",
        "id": c.container_id,
        "target_id": target_id,
        "device_id": device_id,
        "name": f"{host_label}/{c.name}",
        "metric": "container_status",
        "value": 1 if state != "running" else 0,
        "message": f"容器 {c.name} 当前状态异常：{'Restarting' if restarting else (c.status or state or '未知状态')}",
    }


def evaluate_container_alerts() -> None:
    db = SessionLocal()
    try:
        device_rows = (
            db.query(DeviceContainer, Device)
            .join(Device, Device.id == DeviceContainer.device_id)
            .all()
        )
        # PVE 虚拟机的容器经 pve_guest_binding_id 归属,不对应 devices 表,
        # 宿主身份用合成负数 target_id(与主机状态/自动化同一契约)。
        guest_rows = (
            db.query(DeviceContainer, PveGuestBinding, PveConnection)
            .join(
                PveGuestBinding,
                PveGuestBinding.id == DeviceContainer.pve_guest_binding_id,
            )
            .join(PveConnection, PveConnection.id == PveGuestBinding.connection_id)
            .all()
        )
        resources = []
        for c, d in device_rows:
            resources.append(
                _container_resource(
                    c,
                    target_id=d.id,
                    device_id=d.id,
                    host_label=d.name,
                )
            )
        for c, binding, conn in guest_rows:
            target_id = -(conn.id * 1_000_000 + binding.vmid)
            host_label = f"{conn.name}/{binding.guest_type.upper()} VM {binding.vmid}"
            resources.append(
                _container_resource(
                    c,
                    target_id=target_id,
                    device_id=None,
                    host_label=host_label,
                )
            )
    finally:
        db.close()
    _evaluate_resource_rules(resources)


def _interface_failure_note(iface: ServiceInterface, probe: InterfaceProbe) -> str:
    """接口异常话术:直接给出探测结论(状态码/错误文本)。"""
    reason = (probe.error or "").strip()
    if not reason:
        reason = f"状态码 {probe.status_code}" if probe.status_code else "探测失败"
    return f"接口「{iface.name}」{reason}"


def evaluate_business_alerts() -> None:
    db = SessionLocal()
    try:
        businesses = db.query(Business).all()
        statuses = get_latest_statuses()
        now = datetime.now(timezone.utc)
        # 三条总查询代替逐业务两条(旧实现 N 个业务 = 2N 条查询)，
        # 按 business_id 分组后聚合口径不变。虚拟机只读内存快照
        # (guest_link_state)，绝不在评估路径触发 PVE 调用。
        server_rows = (
            db.query(BusinessServer, Device)
            .join(Device, Device.id == BusinessServer.device_id)
            .all()
        )
        guest_rows = db.query(BusinessPveGuest).all()
        iface_rows = (
            db.query(BusinessInterface, ServiceInterface, InterfaceProbe)
            .join(
                ServiceInterface,
                ServiceInterface.id == BusinessInterface.interface_id,
            )
            .outerjoin(
                InterfaceProbe, InterfaceProbe.interface_id == ServiceInterface.id
            )
            .filter(ServiceInterface.enabled == 1)
            .all()
        )
        servers_by_biz: dict[int, list[Device]] = {}
        for _bs, device in server_rows:
            servers_by_biz.setdefault(_bs.business_id, []).append(device)
        guests_by_biz: dict[int, list[BusinessPveGuest]] = {}
        for link in guest_rows:
            guests_by_biz.setdefault(link.business_id, []).append(link)
        # 接口按 (ServiceInterface, InterfaceProbe|None) 成对保留:探测详情
        # (状态码/错误文本/checked_at)要进异常明细与 sampled_at 回溯。
        ifaces_by_biz: dict[
            int, list[tuple[ServiceInterface, InterfaceProbe | None]]
        ] = {}
        for _bi, iface, probe in iface_rows:
            ifaces_by_biz.setdefault(_bi.business_id, []).append((iface, probe))
        resources = []
        for b in businesses:
            servers = servers_by_biz.get(b.id, [])
            guests = guests_by_biz.get(b.id, [])
            ifaces = ifaces_by_biz.get(b.id, [])
            device_online = sum(
                1 for d in servers if statuses.get(d.id, d.status) == "online"
            )
            up = sum(1 for _i, p in ifaces if p and p.up == 1)
            # 虚拟机口径与业务面板(routers/businesses)一致:PVE 连接不可达=
            # 状态未知，不计入健康分母。旧实现完全不含虚机，纯虚机业务的
            # total=0 永远 value=0(正常)——全部虚机停机也不会触发告警。
            guest_states = [
                guest_link_state(link.connection_id, link.guest_type, link.vmid)
                for link in guests
            ]
            guest_total, guest_online = judged_guest_health(guest_states)
            unknown_guests = len(guest_states) - guest_total
            server_total = len(servers) + guest_total
            server_online = device_online + guest_online
            total = server_total + len(ifaces)
            health = 0 if total == 0 or server_online + up == total else 1
            message = (
                f"业务 {b.name} 状态异常：服务器 {server_online}/{server_total} 在线，"
                f"接口 {up}/{len(ifaces)} 正常"
            )
            if unknown_guests:
                message += f"；另有 {unknown_guests} 台虚拟机状态未知"
            # 异常明细(2026-09-18 用户定调):直接指出是哪台服务器离线、
            # 哪个接口返回了什么状态码——汇总计数知道"有事",明细才知道
            # "去哪看"。异常成员的探测/快照时刻同时收集,作 sustain 回溯。
            details: list[str] = []
            evidence: list[datetime] = []
            for d in servers:
                if statuses.get(d.id, d.status) != "online":
                    details.append(
                        f"服务器 {d.name}({d.ip_address}) 离线"
                        if d.ip_address
                        else f"服务器 {d.name} 离线"
                    )
            for link, (online, reachable) in zip(guests, guest_states, strict=False):
                label = link.guest_name or f"VM {link.vmid}"
                if not reachable:
                    details.append(f"虚拟机 {label} 状态未知(平台不可达)")
                elif not online:
                    details.append(f"虚拟机 {label} 离线或未运行")
                    state = get_connection_state(link.connection_id) or {}
                    checked = state.get("checked_at")
                    if checked:
                        evidence.append(
                            datetime.fromtimestamp(checked, tz=timezone.utc)
                        )
            for iface, probe in ifaces:
                if probe is None:
                    details.append(f"接口「{iface.name}」暂无探测结果")
                    continue
                if probe.up == 1:
                    continue
                details.append(_interface_failure_note(iface, probe))
                if probe.checked_at is not None:
                    evidence.append(probe.checked_at)
            if details and health == 1:
                # 明细另起一行(2026-09-18 用户要求):汇总句保持单行,每条
                # 异常成员独立一行——飞书卡片/告警中心(white-space: pre-line)
                # 都按换行符渲染。
                shown = "\n".join(details[:3])
                more = f" 等 {len(details)} 项异常" if len(details) > 3 else ""
                message += f"\n异常明细：{shown}{more}"
            # sustain 回溯:任一成员失败业务即异常,回溯到最早的新鲜异常
            # 证据时刻(探测 checked_at/快照 checked_at)——消掉"评估周期
            # 错相位"白等的 sustain;证据过期(探测停摆)时退回 now,不回溯。
            sampled = None
            if health == 1:
                fresh = [
                    ts
                    for ts in evidence
                    if (now - ts).total_seconds() <= 2 * BUSINESS_ALERT_INTERVAL
                ]
                if fresh:
                    sampled = min(fresh)
            resources.append(
                {
                    "type": "business",
                    "id": b.id,
                    "name": b.name,
                    "business_id": b.id,
                    "metric": "business_status",
                    "value": health,
                    "message": message,
                    "sampled_at": sampled,
                }
            )
    finally:
        db.close()
    _evaluate_resource_rules(resources)


def _claim_notification(db: Session, event_id: int) -> bool:
    """发送前的原子占位:把 notification_status 从 pending 翻成 sent,返回是否抢到。

    「pending」是"本轮该发"的唯一信号——评估器对首次通知/冷却重发/恢复通知
    入队前都会先把状态置回 pending。占位=条件 UPDATE(pending→sent)原子完成:
    两个评估实例(本地开发进程+容器共用同一库,Redis leader 锁切换期间可能
    短暂双跑)同轮各产生一次通知时,先占位者投递,后到者 0 行放弃,飞书只收
    到一张卡(2026-09-17 实测连续两条相同告警卡)。

    只看状态、不看时间戳:alert.resolved 是一次性生命周期通知,绝不能拿
    created 卡的 last_notified_at 做冷却判断——2026-09-18 实测:created 发出
    4.5 分钟后事件恢复,规则冷却 5 分钟未满,恢复卡被旧占位条件吞掉,状态
    永远挂着「待通知」。冷却重发的时机判断归评估器(内存里 cooldown 到期
    才会置 pending 入队),跨实例竞态由这里的原子翻转兜住。
    """
    updated = (
        db.query(AlertEvent)
        .filter(AlertEvent.id == event_id, AlertEvent.notification_status == "pending")
        .update(
            {
                AlertEvent.notification_status: "sent",
                AlertEvent.last_notified_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return bool(updated)


def _notify_event_in_background(
    event_id: int,
    event_name: str,
    *,
    track: bool = True,
) -> None:
    """Send webhook after the evaluation transaction has committed."""
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if not event:
            return
        # 静默窗口:窗口内对象的通知在此出口处统一拦截(连 resolved 一起静默:
        # 没见过告警的恢复通知更莫名其妙)。事件本身照常流转,窗口结束后
        # 持续异常的告警由冷却重发浮出。track 时标 skipped,告警中心可见口径。
        window = _active_mute_window(db, event)
        if window is not None:
            window_id = window.id
            if track:
                event.notification_status = "skipped"
                db.commit()
            _count_muted(window_id)
            return
        # 跨实例去重(原子占位):抢不到说明另一实例刚发过同一张卡,直接放弃。
        # 首次通知(pending→首投)同样要占位——双实例的首次投递竞态同样存在。
        # 放在静默检查之后:被静默拦下的通知不占额度。
        # 恢复/进度类(track=False)不做占位:它们本来就允许多次推。
        if track and not _claim_notification(db, event_id):
            return
        # 归因类告警(cpu/mem/disk)整生命周期只发一张卡(2026-09-16
        # 用户定调):触发卡已被扣住直到带结论放行,恢复再推一张 alert.resolved
        # 只会把同样的详情+归因重铺一遍(实测:停用规则自动关停也推)。恢复状态
        # 由告警中心展示。host_status/business 等其它告警保持 created+resolved
        # 两条不变(离线→恢复的闭环信息仍然需要)。
        # container_status(2026-09-17):容器与主机同为两步卡片模型,恢复卡
        # 携带「自动处置」结论闭环,不再静默;但强制收尾(规则停用/删除等,
        # _close_event 写入 force_closed)没有真实恢复,卡片纯属噪音,仍静默。
        force_closed = bool((event.remediation_json or {}).get("force_closed"))
        if event_name == "alert.resolved" and (
            event.metric in _RESOLVED_QUIET_METRICS
            or (event.metric in ANALYSIS_METRICS and force_closed)
        ):
            if track:
                # 评估器已把状态置 pending,标记为 sent 避免告警中心挂着「待通知」
                event.notification_status = "sent"
                event.last_notified_at = datetime.now(timezone.utc)
                db.commit()
            return
        rule = db.query(AlertRule).filter(AlertRule.id == event.rule_id).first()
        device = (
            db.query(Device).filter(Device.id == event.device_id).first()
            if event.device_id
            else None
        )
        # 规则可能已被删除(事件历史保留、rule_id 置空)，_payload 会用规则名快照兜底。
        _notify(db, event, rule, device, event_name, track=track)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Alert webhook delivery failed for event %s", event_id)
    finally:
        db.close()


def notify_event(
    event_id: int,
    event_name: str,
    *,
    track: bool = True,
) -> None:
    """把一次 Webhook 投递排入后台队列;必须在事件事务提交之后调用。

    跨实例去重由 _notify_event_in_background 里的原子占位完成(pending→sent),
    冷却重发的时机判断归评估器,这里不再需要冷却参数。
    """
    _notification_pool.submit(
        _notify_event_in_background,
        event_id,
        event_name,
        track=track,
    )


# ── 事件收尾:规则停用/删除、对象移出范围、对象已被删除 ──
#
# 评估器只会恢复"本轮还覆盖得到"的事件。以下四种情况之后，遗留的 open 事件再也不会
# 被任何一轮评估访问到，于是永远挂着"未恢复":告警中心一直显示未恢复，概览的
# open_events / active_rules 被死数据顶着，订阅方也等不到 alert.resolved。
#   * 规则被停用(评估器只加载 enabled 的规则);
#   * 规则被删除;
#   * 对象被移出规则的 target_device_ids / target_container_ids / target_business_ids;
#   * 对象整行被删除(设备 / PVE 平台 / 业务 / 容器)。
# 判定只依据数据库里能证明的事实，绝不因为"这轮没采到"或"对象暂时不可达"就关告警——
# 采集失败的设备恰恰是最需要保持告警的。

RULE_DISABLED_NOTE = "告警规则已停用，事件自动关闭"
RULE_DELETED_NOTE = "告警规则已删除，事件自动关闭"
RULE_MISSING_NOTE = "告警规则已不存在，事件自动关闭"
TARGET_OUT_OF_SCOPE_NOTE = "监控对象已移出该告警规则的范围，事件自动关闭"
TARGET_DELETED_NOTE = "监控对象已被删除，事件自动关闭"

# ── 已知晓(snooze):单运维告警降噪 ──
#
# 点一下「已知晓」后,该事件冷却到期不再重发 alert.created(评估/归因/
# 自动处置照常),恢复时正常推 alert.resolved 闭环不断。站内按钮走登录态,
# 飞书卡片按钮走免鉴权链接(HMAC 签名,仅对该事件+有效期,无法伪造)。

SNOOZED_NOTE = "已知晓，恢复前不再重复提醒"


def snooze_event(event_id: int, *, by_name: str | None = None) -> AlertEvent | None:
    """置为已知晓(幂等):已 resolved 的事件不拒绔,标记仍有留痕价值。

    返回更新后的事件(供响应构造);事件不存在返回 None。"""
    db = SessionLocal()
    try:
        event = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
        if event is None:
            return None
        if not event.snoozed:
            event.snoozed = True
            event.snoozed_at = datetime.now(timezone.utc)
            event.snoozed_by_name = by_name
            db.commit()
        db.refresh(event)
        return event
    finally:
        db.close()


# ── 静默窗口(维护期免轰炸) ──


def _event_owner_for_window(event: AlertEvent) -> int | None:
    """事件宿主身份(与 owner_target_id 同源,兼容旧行):设备正 id / 虚机负数 id。"""
    if event.device_id is not None:
        return event.device_id
    if event.owner_target_id is not None:
        return event.owner_target_id
    # 兼容旧格式:处置记录 target_id > 负数 resource_id
    owner = _int_or_none((event.remediation_json or {}).get("target_id"))
    if owner is not None:
        return owner
    resource = _int_or_none(event.resource_id)
    return resource if resource is not None and resource < 0 else None


def _active_mute_window(db: Session, event: AlertEvent):
    """事件对象当前命中的静默窗口;命中即返回窗口(调用方跳过通知并计数)。"""
    from app.models.maintenance_window import MaintenanceWindow

    now = datetime.now(timezone.utc)
    windows = (
        db.query(MaintenanceWindow)
        .filter(
            MaintenanceWindow.enabled.is_(True),
            MaintenanceWindow.start_at <= now,
            MaintenanceWindow.end_at > now,
        )
        .all()
    )
    if not windows:
        return None
    owner = _event_owner_for_window(event)
    for window in windows:
        targets = window.target_ids
        # 目标为空 = 全部对象;
        # 指定了目标 = 宿主身份命中(业务事件无宿主身份,只在「全部」时静默)
        if not targets:
            return window
        if owner is not None and owner in targets:
            return window
    return None


def _count_muted(window_id: int) -> None:
    """窗口计数 +1(独立事务,失败不阻断通知跳过本身)。"""
    from app.models.maintenance_window import MaintenanceWindow

    db = SessionLocal()
    try:
        db.query(MaintenanceWindow).filter(MaintenanceWindow.id == window_id).update(
            {MaintenanceWindow.muted_count: MaintenanceWindow.muted_count + 1}
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to count muted notification for window %s", window_id)
    finally:
        db.close()


def sweep_maintenance_summaries() -> int:
    """看门狗:已结束且未汇总的窗口推一条汇总卡,返回本次汇总的窗口数。

    汇总卡发给订阅 alert.created 的钩子(静默吞掉的正是他们的告警)。
    muted_count=0 的窗口也发(告知「静默期无告警」同样是有效信息,变更圆满)。
    """
    from app.models.maintenance_window import MaintenanceWindow

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        windows = (
            db.query(MaintenanceWindow)
            .filter(
                MaintenanceWindow.enabled.is_(True),
                MaintenanceWindow.end_at <= now,
                MaintenanceWindow.summary_state == "",
            )
            .all()
        )
    except Exception:
        db.rollback()
        db.close()
        logger.exception("Failed to scan maintenance windows")
        return 0
    db.close()

    summarized = 0
    for window in windows:
        summary = (
            f"维护窗口「{window.name}」已结束，静默期间共抑制了 "
            f"{window.muted_count} 次告警通知。"
            + (
                "窗口内无告警被抑制，变更期间无异常。"
                if window.muted_count == 0
                else "如异常仍在持续，相关告警将在下一轮冷却到期后自动恢复推送。"
            )
        )
        payload = {
            "event": "alert.maintenance_summary",
            "alert": {
                "severity": "info",
                "status": "resolved",
                "rule_name": f"维护窗口 · {window.name}",
                "metric": "maintenance",
                "metric_label": "维护静默",
                "message": summary,
                "first_triggered_at": _utc_iso(window.start_at),
                "last_seen_at": _utc_iso(window.end_at),
                "resolved_at": _utc_iso(window.end_at),
            },
            "device": {
                "id": None,
                "name": window.name,
                "ip_address": None,
                "type": "window",
            },
            "resource": {"type": "window", "id": str(window.id), "name": window.name},
            "timestamp": now.isoformat(),
            "source": "DCN",
        }
        payload.update(
            {
                "rule_name": payload["alert"]["rule_name"],
                "severity": "info",
                "message": summary,
                "device_name": window.name,
            }
        )
        hooks_db = SessionLocal()
        try:
            hooks = [
                w
                for w in hooks_db.query(Webhook).filter(Webhook.enabled.is_(True)).all()
                if "alert.created" in (w.events or [])
                # 级别路由:汇总卡 severity=info,配置了级别过滤的钩子按其过滤
                and (
                    not (w.config or {}).get("severities")
                    or "info" in (w.config or {}).get("severities")
                )
            ]
        finally:
            hooks_db.close()
        if hooks:
            futures = [
                _notification_pool.submit(_deliver_one, hook, payload) for hook in hooks
            ]
            for future in as_completed(futures):
                future.result()
        # 标记已汇总(独立事务)
        db2 = SessionLocal()
        try:
            db2.query(MaintenanceWindow).filter(
                MaintenanceWindow.id == window.id
            ).update({MaintenanceWindow.summary_state: "summarized"})
            db2.commit()
        except Exception:
            db2.rollback()
            logger.exception("Failed to mark summary for window %s", window.id)
        finally:
            db2.close()
        summarized += 1
        logger.info(
            "Maintenance window %s summarized (%d muted)", window.id, window.muted_count
        )
    return summarized


def _int_or_none(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _event_container_owner(event: AlertEvent) -> int | None:
    """容器事件的宿主 target_id:设备为正 id,虚拟机取创建时存入处置记录的负数 id。"""
    if event.device_id is not None:
        return event.device_id
    return _int_or_none((event.remediation_json or {}).get("target_id"))


def _event_target_id(event: AlertEvent) -> int | None:
    """事件的目标 id:正数是 devices.id，负数是 PVE guest 编码，容器/业务另按类型判。"""
    target = _int_or_none(event.resource_id)
    return event.device_id if target is None else target


def _rule_covers(rule: AlertRule, event: AlertEvent) -> bool:
    """规则当前的监控范围是否还包含这个事件的对象。

    与两个评估器里的跳过条件保持一致:范围为空表示"全部对象"。
    """
    metric = rule.metric or ""
    # container_/host_ 前缀先于 ANALYSIS_METRICS 判:container_status 现在既是
    # 归因指标又是容器指标,若先走 ANALYSIS 分支会把容器 id 当 target id 比,
    # 误判「规则不再覆盖」而误关告警。
    if metric.startswith("container_"):
        devices = set(rule.target_device_ids or ())
        containers = {str(v) for v in (rule.target_container_ids or ())}
        if not devices and not containers:
            return True
        if str(event.resource_id or "") in containers:
            return True
        return _event_container_owner(event) in devices
    if metric.startswith("host_"):
        targets = set(rule.target_device_ids or ())
        return not targets or _event_target_id(event) in targets
    if metric in ANALYSIS_METRICS:
        targets = set(rule.target_device_ids or ())
        if not targets:
            return True
        # 虚拟机事件的 device_id 为 None、宿主身份在 resource_id(负数);
        # 设备事件的 device_id 与 resource_id 一致,统一按 target id 判。
        return _event_target_id(event) in targets
    if metric.startswith("business_"):
        targets = set(rule.target_business_ids or ())
        return not targets or _int_or_none(event.resource_id) in targets
    # 认不出来的指标不要擅自关告警。
    return True


def _target_exists(event: AlertEvent, snapshot: dict) -> bool:
    """监控对象在平台里是否还存在。

    清单类判据(容器、PVE 虚机)每轮采集都会整表重写，探测失败时同样会写空，所以
    "清单里没有它"只有在"这轮探测确实成功"的前提下才等于对象真的没了，否则一次
    SSH 抖动或 PVE 不可达就会把最需要保持的告警误清成已恢复:

    * 容器看 ``device_docker_status.available=1``;
    * PVE 虚机看 ``pve_guest_status`` 的连接状态 ``reachable=True`` 且快照足够新。

    拿不到可信清单时一律判定"还在"，宁可晚一轮收敛也不误关。
    """
    resource_type = event.resource_type or "device"
    if resource_type == "business":
        return _int_or_none(event.resource_id) in snapshot["business_ids"]
    if resource_type == "container":
        owner = _event_container_owner(event)
        if owner is None:
            # 拿不到宿主身份(旧格式事件),按 docker 状态无法定位,保持不收敛
            return True
        if owner not in snapshot["docker_ready"]:
            return True
        return (owner, str(event.resource_id or "")) in snapshot["container_keys"]
    target_id = _event_target_id(event)
    if target_id is None:
        return True
    if target_id < 0:
        decoded = decode_pve_target_id(target_id)
        if decoded is None:
            return True
        connection_id, vmid = decoded
        if connection_id not in snapshot["connection_ids"]:
            # 整个 PVE 平台都被删了，这是数据库能证明的事实。
            return False
        guests = snapshot["pve_guests"].get(connection_id)
        if guests is None:
            return True
        return vmid in guests
    return target_id in snapshot["device_ids"]


# PVE guest 快照比这还旧就当作"证明不了"。刷新循环每 PVE_STATUS_INTERVAL 跑一轮，
# 多个平台串起来一轮会更久，这里留足冗余。
_PVE_SNAPSHOT_MAX_AGE = max(120.0, PVE_STATUS_INTERVAL * 4.0)


def _trusted_pve_guests(connection_ids: set[int]) -> dict[int, set[int]]:
    """本轮确实探测成功的 PVE 连接 → 它名下的 vmid 集合。

    只有 ``reachable=True`` 且 ``checked_at`` 足够新的连接才进这个字典。进程刚启动、
    本实例不是 leader、或 PVE 连不上时，对应连接不会出现在返回值里，调用方据此
    区分"清单里没有它"和"这轮根本没拿到清单"。
    """
    now = time.time()
    trusted: dict[int, set[int]] = {}
    for connection_id in connection_ids:
        state = get_connection_state(connection_id)
        if not state or not state.get("reachable"):
            continue
        if now - float(state.get("checked_at") or 0.0) > _PVE_SNAPSHOT_MAX_AGE:
            continue
        trusted[connection_id] = set(get_guests_by_connection(connection_id))
    return trusted


def _container_keys(db: Session) -> set[tuple[int, str]]:
    """(宿主 target_id, container_id) 集合,覆盖设备与 PVE 虚拟机两类归属。"""
    keys: set[tuple[int, str]] = set()
    rows = db.query(
        DeviceContainer.device_id,
        DeviceContainer.container_id,
        PveGuestBinding.connection_id,
        PveGuestBinding.vmid,
    ).outerjoin(
        PveGuestBinding,
        PveGuestBinding.id == DeviceContainer.pve_guest_binding_id,
    )
    for device_id, container_id, connection_id, vmid in rows.all():
        if device_id is not None:
            keys.add((device_id, str(container_id)))
        elif connection_id is not None and vmid is not None:
            keys.add((-(connection_id * 1_000_000 + vmid), str(container_id)))
    return keys


def _docker_ready_targets(db: Session) -> set[int]:
    """本轮 Docker 探测成功的宿主 target_id 集合(设备正 id / 虚拟机合成负数 id)。"""
    ready: set[int] = set()
    rows = (
        db.query(
            DeviceDockerStatus.device_id,
            PveGuestBinding.connection_id,
            PveGuestBinding.vmid,
        )
        .outerjoin(
            PveGuestBinding,
            PveGuestBinding.id == DeviceDockerStatus.pve_guest_binding_id,
        )
        .filter(DeviceDockerStatus.available == 1)
    )
    for device_id, connection_id, vmid in rows.all():
        if device_id is not None:
            ready.add(device_id)
        elif connection_id is not None and vmid is not None:
            ready.add(-(connection_id * 1_000_000 + vmid))
    return ready


def _target_snapshot(db: Session) -> dict:
    connection_ids = {row[0] for row in db.query(PveConnection.id).all()}
    return {
        "device_ids": {row[0] for row in db.query(Device.id).all()},
        "connection_ids": connection_ids,
        "business_ids": {row[0] for row in db.query(Business.id).all()},
        "container_keys": _container_keys(db),
        "docker_ready": _docker_ready_targets(db),
        "pve_guests": _trusted_pve_guests(connection_ids),
    }


def _close_event(db: Session, event: AlertEvent, note: str) -> bool:
    """把一条 open 事件收尾成 resolved，返回是否需要推 alert.resolved。"""
    record = dict(event.remediation_json or {})
    # 扣住等归因的通知一并丢弃:规则/对象都没了，再补一对 created/resolved 只是噪音，
    # 留着这个标记还会让看门狗在事件已 resolved 之后又把 created 发出去。
    held = record.pop("notification_held", None)
    record.pop("notification_held_at", None)
    # 强制收尾(规则停用/删除/对象移出范围)标记:归因类告警的 resolved 卡
    # 在通知出口据此静默——这不是真实恢复,容器等两步卡片模型也不该为它
    # 发"恢复"卡。
    record["force_closed"] = True
    if event.remediation_state in ("running", "verifying"):
        # 处置还没收尾就失去了评估来源，别让"自动处置中"跟着事件一起僵死。
        event.remediation_state = "skipped"
        event.remediation_detail = None
        record["state"] = "skipped"
    event.remediation_json = record
    now = datetime.now(timezone.utc)
    base = record.get("base_message") or event.message
    event.status = "resolved"
    event.resolved_at = now
    event.last_seen_at = now
    apply_message(event, f"{base}；{note}")
    if held:
        event.notification_status = "skipped"
        return False
    event.notification_status = "pending"
    return True


def _close_events(db: Session, events: list[AlertEvent], note: str) -> int:
    """收尾一批事件并推送 alert.resolved，返回关闭掉的 open 事件条数。

    与评估器"指标恢复正常"时的收尾保持一致:pending(持续时长未满、从没对外触发过)
    直接删掉不留历史;open 置为 resolved 并通知订阅方。

    本函数自行 commit:紧接着要按事件推 Webhook，而通知线程在独立会话里读事件，
    必须先落库才看得到 resolved 状态。
    """
    if not events:
        return 0
    closed = 0
    deleted = 0
    notify_ids: list[int] = []
    for event in events:
        if event.status == "pending":
            db.delete(event)
            deleted += 1
            continue
        closed += 1
        if _close_event(db, event, note):
            notify_ids.append(event.id)
    db.commit()
    if deleted:
        logger.info("Dropped %d pending alert events (%s)", deleted, note)
    for event_id in notify_ids:
        notify_event(event_id, "alert.resolved")
    return closed


def _active_events_of_rule(db: Session, rule: AlertRule) -> list[AlertEvent]:
    return (
        db.query(AlertEvent)
        .filter(
            AlertEvent.rule_id == rule.id,
            AlertEvent.status.in_(("pending", "open")),
        )
        .all()
    )


def close_events_of_rule_out_of_scope(db: Session, rule: AlertRule) -> int:
    """收尾“被移出本规则监控范围”的事件，返回关闭的 open 事件条数。

    与 ``close_orphaned_alert_events`` 的全表扫描不同，这里只看这一条规则的
    事件——更新规则范围时不用为其它规则的孤儿买单(它们白有周期性 sweep 兑底)，
    免去一次全表扫。判定复用 ``_rule_covers``，与评估器的跳过条件同口径。
    """
    events = [
        event
        for event in _active_events_of_rule(db, rule)
        if not _rule_covers(rule, event)
    ]
    return _close_events(db, events, TARGET_OUT_OF_SCOPE_NOTE)


def close_events_of_disabled_rule(db: Session, rule: AlertRule) -> int:
    """收尾已停用规则遗留的事件，返回关闭掉的 open 事件条数。"""
    return _close_events(db, _active_events_of_rule(db, rule), RULE_DISABLED_NOTE)


def close_events_of_deleted_rule(db: Session, rule: AlertRule) -> int:
    """删除规则前收尾它的事件，并把规则名快照留给历史。

    规则是配置，事件是运维留痕:删掉一条配错的规则不该连带销毁它产生过的告警历史。
    收尾后 ``rule_id`` 由外键 ``ON DELETE SET NULL`` 置空，``rule_name`` 保住可读性。
    """
    closed = _close_events(db, _active_events_of_rule(db, rule), RULE_DELETED_NOTE)
    # 规则名在事件创建时就已快照(改名前后的历史各自准确)，这里只补空值。
    db.query(AlertEvent).filter(
        AlertEvent.rule_id == rule.id, AlertEvent.rule_name.is_(None)
    ).update({AlertEvent.rule_name: rule.name}, synchronize_session=False)
    db.commit()
    return closed


def close_orphaned_alert_events() -> int:
    """收尾失去评估来源的告警事件，返回关闭条数。

    启动时跑一遍清历史欠账，之后每个采集周期跑一遍(见 metrics_collector)，
    所以删设备/删业务/删 PVE 平台/改规则范围最多一分钟后就会被收敛，
    不必等下一次重启。
    """
    db = SessionLocal()
    try:
        events = (
            db.query(AlertEvent)
            .filter(AlertEvent.status.in_(("pending", "open")))
            .all()
        )
        if not events:
            return 0
        rules = {rule.id: rule for rule in db.query(AlertRule).all()}
        snapshot = _target_snapshot(db)
        missing_rule: list[AlertEvent] = []
        disabled: list[AlertEvent] = []
        gone: list[AlertEvent] = []
        out_of_scope: list[AlertEvent] = []
        for event in events:
            rule = rules.get(event.rule_id) if event.rule_id is not None else None
            if rule is None:
                missing_rule.append(event)
                continue
            if not rule.enabled:
                # 正常路径下 stop 那一刻就由 close_events_of_disabled_rule 收尾了;
                # 这里兜住历史欠账和绕过接口直接改库停用的情况。
                disabled.append(event)
                continue
            if not _target_exists(event, snapshot):
                gone.append(event)
            elif not _rule_covers(rule, event):
                out_of_scope.append(event)
    except Exception:
        db.rollback()
        logger.exception("Failed to scan orphaned alert events")
        db.close()
        return 0
    closed = _close_events(db, missing_rule, RULE_MISSING_NOTE)
    closed += _close_events(db, disabled, RULE_DISABLED_NOTE)
    closed += _close_events(db, gone, TARGET_DELETED_NOTE)
    closed += _close_events(db, out_of_scope, TARGET_OUT_OF_SCOPE_NOTE)
    db.close()
    if closed:
        logger.warning("Closed %d orphaned alert events", closed)
    return closed


def cleanup_old_alert_events(retention_days: int = 90) -> int:
    """清理已解决超过 ``retention_days`` 的告警事件,返回删除条数。

    告警事件表随时间无限增长会拖慢告警中心查询;只删「已解决」的,
    open/pending 事件永远保留。每天由后台循环跑一次(见 main.py)。
    """
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.status == "resolved",
                AlertEvent.resolved_at.isnot(None),
                AlertEvent.resolved_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info(
                "Cleaned up %d resolved alert events older than %d days",
                deleted,
                retention_days,
            )
        return deleted
    except Exception:
        db.rollback()
        logger.exception("Failed to clean up old alert events")
        return 0
    finally:
        db.close()


async def run_business_alert_loop():
    """业务状态告警独立评估循环(2026-09-17 起)。

    原先挂在指标采集循环里,METRICS_ENABLED=false 的部署(纯接口监控)会让
    业务告警静默失效。业务健康的输入是 monitor 状态/接口探测/PVE 快照,与
    指标采集无关,独立成环。评估频率与旧挂靠保持一致(60s)。
    """
    logger.info(
        "Business alert evaluator started (interval=%ds)", BUSINESS_ALERT_INTERVAL
    )
    while True:
        try:
            await asyncio.to_thread(evaluate_business_alerts)
        except asyncio.CancelledError:
            raise  # 正常关闭
        except Exception:
            logger.exception("Business alert evaluation failed")
        await asyncio.sleep(BUSINESS_ALERT_INTERVAL)
