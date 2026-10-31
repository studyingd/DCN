"""虚拟机事件的宿主地址解析(从 alerts.py 拆出,2026-10-31 Phase-1)。

运维绑定 IP > QGA 实时地址(5 分钟缓存) > 最近已知 IP 的三层口径,
全量契约见 AGENTS.md「虚机告警的地址解析口径」。
build_client/guest_agent_summary 是测试 patch seam——_guest_qga_ip 内部
经延迟导入的 alerts 门面解析(alerts.* patch 语义不变);_qga_ip_cache
等状态经门面 re-export 后仍是同一对象(测试 .clear() 语义不变)。
"""

import logging
import threading
import time

from sqlalchemy.orm import Session

from app.models.alert import AlertEvent
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.services.containers_collector import decode_pve_target_id

logger = logging.getLogger(__name__)


def _int_or_none(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


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
    from app.services import alerts as _alerts
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
        client = _alerts.build_client(conn)
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
            summary = _alerts.guest_agent_summary(client, str(node), gtype, vmid)
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
