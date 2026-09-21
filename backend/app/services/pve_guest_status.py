"""PVE 虚拟机/容器状态的进程内快照。

业务监控等模块需要频繁读取 guest 运行状态，而 PVE API 单次调用可能耗时数秒
(连接不可达时最长阻塞到 timeout)。这里由后台循环周期性拉取
``/cluster/resources`` 并缓存到内存，读取方永远只做内存查询；PVE 不可达时保留
上一次快照并把连接标记为 ``reachable=False``，界面据此显示"状态未知"。

与 ``app.services.monitor`` 的设备状态缓存同一模式：仅 leader 实例刷新，多副本
部署时其余实例读到空快照(业务侧按"状态未知"降级)。
"""

import asyncio
import logging
import threading
import time
from collections import deque

from sqlalchemy.orm import Session

from app.config import (
    GUEST_IP_REFRESH_INTERVAL,
    PVE_STATUS_INTERVAL,
    PVE_STATUS_TIMEOUT,
)
from app.database import SessionLocal
from app.models.business import BusinessPveGuest
from app.models.pve_connection import PveConnection
from app.services.pve import (
    agent_interfaces_ip,
    build_client,
    is_guest_template,
)
from app.services.pve_guest_rates import compute_rates

logger = logging.getLogger(__name__)

# (connection_id, vmid) -> guest 快照
_guests: dict[tuple[int, int], dict] = {}
# (connection_id, vmid) -> 近 N 轮指标样本(cpu/mem 快照历史,内存环)
# PVE 快照本身只保留最新一轮;Agent 归因 / get_metrics 需要「近 1 小时趋势」
# (区分业务增长与突发泄漏)时从这里取,零额外 API 开销。仅 leader 实例有数据,
# 重启后从空开始重新积累。
_history: dict[tuple[int, int], deque] = {}
# 快照周期 30s,120 样本 ≈ 1 小时
_HISTORY_MAXLEN = 120
# connection_id -> {"name", "reachable", "error", "checked_at"}
_connection_states: dict[int, dict] = {}
_lock = threading.Lock()


def get_guest_snapshot(connection_id: int, vmid: int) -> dict | None:
    """单个 guest 的最新快照；尚未采集到时返回 None。"""
    with _lock:
        item = _guests.get((connection_id, vmid))
        return dict(item) if item else None


def get_guests_by_connection(connection_id: int) -> dict[int, dict]:
    """某连接下全部 guest 快照，键为 vmid。"""
    with _lock:
        return {
            vmid: dict(item)
            for (conn_id, vmid), item in _guests.items()
            if conn_id == connection_id
        }


def get_connection_state(connection_id: int) -> dict | None:
    """连接可达性状态；None 表示尚未采集过。"""
    with _lock:
        state = _connection_states.get(connection_id)
        return dict(state) if state else None


def guest_link_state(connection_id: int, guest_type: str, vmid: int) -> tuple[int, int]:
    """业务关联虚拟机的 (online, reachable)，只读内存快照，绝不触发 PVE 调用。

    routers/businesses 的展示口径与 alerts 的业务告警口径共用此实现
    (2026-09-17 起):连接不可达 = 状态未知;快照 guest_type 与关联行不一致
    (vmid 被复用/类型变更)同样按未知处理。
    """
    state = get_connection_state(connection_id) or {}
    reachable = 1 if state.get("reachable") else 0
    snap = get_guest_snapshot(connection_id, vmid)
    if snap and snap.get("guest_type") and snap.get("guest_type") != guest_type:
        snap = None
    online = 1 if reachable and snap and snap.get("status") == "running" else 0
    return online, reachable


def judged_guest_health(states: list[tuple[int, int]]) -> tuple[int, int]:
    """由 (online, reachable) 列表得出计入健康分母的 (总数, 在线数)。

    PVE 连接不可达的 guest 状态未知，既不能算正常也不能算异常，因此不计入
    健康分母;界面仍展示关联总数并把这类条目标记为"状态未知"。
    """
    judged = [state for state in states if state[1]]
    return len(judged), sum(1 for online, _reachable in judged if online)


# ── guest 最近已知 IP(离线告警的地址兕底) ──
#
# host_status 告警触发时 guest 已停机,QGA(跑在 guest 里)问不出地址;唯一能跨
# 停机持久的是运维接入绑定的 ip_address。QGA-only 虚机没绑定行,告警卡片的
# 「地址」与详情话术里的 IP 一直是 '-'。这里把 guest **运行期间** QGA 报过的
# 地址记在内存里(刷新循环低频维护),停机后作显示兕底——仅显示用,不用于连接。
# 进程内存、仅 leader 实例有数据,重启清空属预期;需要跨重启持久的地址
# 请配置运维接入。
_last_known_ip: dict[tuple[int, int], tuple[str, float]] = {}
# 每 guest 的上次探测结果(是否拿到 IP, 时刻):失败后退避,避免成批 agent
# 未启用的 guest 每轮空耗 4s 超时。
_ip_probe_at: dict[tuple[int, int], tuple[bool, float]] = {}
# 失败探测的重试间隔(成功则每轮都探,一次只要 <1s)
_IP_PROBE_BACKOFF = 30 * 60.0


def record_guest_ip(connection_id: int, vmid: int, ip: str | None) -> None:
    """记下 guest 运行期间 QGA 报出的地址(离线后地址列兕底显示)。"""
    value = str(ip or "").strip()
    if not value:
        return
    with _lock:
        _last_known_ip[(connection_id, vmid)] = (value, time.time())


def get_last_known_ip(
    connection_id: int, vmid: int, *, max_age: float = 24 * 3600.0
) -> str | None:
    """最近一次已知 IP;超过 max_age(默认 24h)视为过期返回 None。"""
    with _lock:
        item = _last_known_ip.get((connection_id, vmid))
    if not item:
        return None
    ip, ts = item
    return ip if time.time() - ts <= max_age else None


def _refresh_guest_ips_sync() -> int:
    """低频刷新一轮 guest IP 缓存(仅 leader 后台循环调用),返回本轮命中数。

    只在存在启用中的 host_status 规则时干活(与 guest_fs_metrics 的磁盘规则
    闸门同一先例):没有主机状态告警就没有消费者,不白发 QGA 请求。
    对每个 running 的 qemu guest 调一次 network-get-interfaces(单命令 4s
    超时;agent 未启用/未安装的 guest 失败后退避 30 分钟再试)。
    """
    from app.models.alert import AlertRule

    db = SessionLocal()
    try:
        has_rules = (
            db.query(AlertRule.id)
            .filter(AlertRule.metric == "host_status", AlertRule.enabled.is_(True))
            .first()
            is not None
        )
        if not has_rules:
            return 0
        updated = 0
        now = time.time()
        for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
            guests = get_guests_by_connection(conn.id)
            if not guests:
                continue
            client = None
            for vmid, guest in guests.items():
                if (
                    str(guest.get("status")) != "running"
                    or guest.get("guest_type") != "qemu"
                ):
                    continue
                node = guest.get("node")
                if not node:
                    continue
                key = (conn.id, vmid)
                with _lock:
                    last = _ip_probe_at.get(key)
                if last and not last[0] and now - last[1] < _IP_PROBE_BACKOFF:
                    continue
                ip = None
                try:
                    if client is None:
                        client = build_client(conn)
                    ip = agent_interfaces_ip(
                        client.guest_agent_interfaces(str(node), "qemu", vmid)
                    )
                except Exception:
                    ip = None
                with _lock:
                    _ip_probe_at[key] = (bool(ip), time.time())
                if ip:
                    record_guest_ip(conn.id, vmid, ip)
                    updated += 1
        return updated
    finally:
        db.close()


async def run_pve_guest_ip_refresh_loop():
    """后台循环:每 GUEST_IP_REFRESH_INTERVAL 秒维护一次 guest 最近已知 IP。"""
    logger.info("Guest IP refresher started (interval=%ds)", GUEST_IP_REFRESH_INTERVAL)
    # 首轮先等快照循环跑完一轮(30s 周期 + 采集余量):刷新读的是内存快照,
    # 启动即查只会看到空快照,白白空转到 5 分钟后的下一轮。
    await asyncio.sleep(PVE_STATUS_INTERVAL + 5)
    while True:
        try:
            updated = await asyncio.to_thread(_refresh_guest_ips_sync)
            if updated:
                logger.debug("Guest IP cache refreshed: %d guests", updated)
        except Exception:
            logger.exception("Guest IP refresh cycle failed")
        await asyncio.sleep(GUEST_IP_REFRESH_INTERVAL)


def get_guest_metric_history(connection_id: int, vmid: int) -> list[dict]:
    """单个 guest 的指标样本历史(旧→新)。每条:{ts, cpu, mem, maxmem, status}。

    cpu 沿用 PVE 口径(0~1 小数),消费方自行×100;未采集或非 leader 实例返回空。
    """
    with _lock:
        hist = _history.get((connection_id, vmid))
        return [dict(item) for item in hist] if hist else []


def _normalize_guest(raw: dict) -> dict | None:
    """把 /cluster/resources 的一条记录归一成内部快照结构；模板机跳过。"""
    if is_guest_template(raw):
        return None
    vmid = raw.get("vmid")
    if vmid is None:
        return None
    # PVE 用 id 前缀区分 guest 类型(如 "qemu/101"、"lxc/200")。
    # LXC 已彻底移除(2026-09-17):快照不纳入 lxc——告警/业务监控/容器采集/
    # 候选列表等所有下游一律不感知。
    raw_id = str(raw.get("id") or "")
    guest_type = (
        raw_id.split("/", 1)[0] if "/" in raw_id else str(raw.get("type") or "")
    )
    if guest_type != "qemu":
        return None
    name = str(raw.get("name") or "").strip()
    return {
        "guest_type": guest_type,
        "vmid": int(vmid),
        "name": name or f"VM {vmid}",
        "node": str(raw.get("node") or "").strip() or None,
        "status": str(raw.get("status") or "unknown"),
        "cpu": float(raw.get("cpu") or 0.0),
        "mem": int(raw.get("mem") or 0),
        "maxmem": int(raw.get("maxmem") or 0),
        "uptime": int(raw.get("uptime") or 0),
    }


def refresh_connection(conn: PveConnection, *, timeout: int | None = None) -> bool:
    """刷新单个连接的 guest 快照；返回该连接是否可达。

    ``timeout`` 按次透传给 ``list_guest_resources``(不回写共享客户端的默认
    超时——客户端是进程级缓存复用的,改了会把所有用户请求路径一并改短)。
    """
    client = build_client(conn)
    try:
        # 请求路径用更短的超时，避免 PVE 不可达时把接口拖到默认 15s。
        kwargs = {"timeout": int(timeout)} if timeout else {}
        raw_guests = client.list_guest_resources(**kwargs)
    except Exception as exc:  # PveError 与底层网络异常统一降级为"不可达"
        logger.warning(
            "PVE guest 状态刷新失败 conn=%s(id=%s): %s", conn.name, conn.id, exc
        )
        with _lock:
            _connection_states[conn.id] = {
                "name": conn.name,
                "reachable": False,
                "error": str(exc),
                "checked_at": time.time(),
            }
        return False

    # 后台循环顺手保温 IO 速率差分基线:即使没人打开页面,
    # 请求路径第一次拿数据时速率就已经是现成的。
    compute_rates(conn.id, raw_guests or [])

    snapshot: dict[tuple[int, int], dict] = {}
    for raw in raw_guests or []:
        guest = _normalize_guest(raw)
        if guest:
            snapshot[(conn.id, guest["vmid"])] = guest

    with _lock:
        for key in [key for key in _guests if key[0] == conn.id]:
            _guests.pop(key, None)
        _guests.update(snapshot)
        now = time.time()
        for key, guest in snapshot.items():
            _history.setdefault(key, deque(maxlen=_HISTORY_MAXLEN)).append(
                {
                    "ts": now,
                    "cpu": guest["cpu"],
                    "mem": guest["mem"],
                    "maxmem": guest["maxmem"],
                    "status": guest["status"],
                }
            )
        # 本轮快照里已不存在的 guest(虚机已销毁)连带清掉历史环/IP 兑底/
        # 探测退避条目:长跑进程里删过的虚机不至于在这些 dict 里无限累积。
        # 失败路径(上面 except 分支)不走这里,PVE 不可达时旧数据照旧保留。
        for store in (_history, _last_known_ip, _ip_probe_at):
            for key in [k for k in store if k[0] == conn.id and k not in snapshot]:
                store.pop(key, None)
        _connection_states[conn.id] = {
            "name": conn.name,
            "reachable": True,
            "error": None,
            "checked_at": now,
        }
    return True


def ensure_fresh(
    db: Session, connection_ids: list[int], *, max_age: float = 60.0
) -> None:
    """请求路径按需补采：只刷新超过 max_age 未采集过的连接。"""
    now = time.time()
    stale: list[int] = []
    with _lock:
        for conn_id in dict.fromkeys(connection_ids):
            state = _connection_states.get(conn_id)
            if not state or now - float(state.get("checked_at") or 0) > max_age:
                stale.append(conn_id)
    if not stale:
        return
    rows = db.query(PveConnection).filter(PveConnection.id.in_(stale)).all()
    for conn in rows:
        refresh_connection(conn, timeout=PVE_STATUS_TIMEOUT)


def _refresh_all_sync() -> None:
    db = SessionLocal()
    try:
        conns = (
            db.query(PveConnection)
            .filter(PveConnection.enabled == 1)
            .order_by(PveConnection.id)
            .all()
        )
        alive = {conn.id for conn in conns}
        for conn in conns:
            refresh_connection(conn)
        # 清理已删除或已停用连接的残留快照
        with _lock:
            for key in [key for key in _guests if key[0] not in alive]:
                _guests.pop(key, None)
            for store in (_history, _last_known_ip, _ip_probe_at):
                for key in [k for k in store if k[0] not in alive]:
                    store.pop(key, None)
            for conn_id in [cid for cid in _connection_states if cid not in alive]:
                _connection_states.pop(conn_id, None)
    finally:
        db.close()


async def refresh_all() -> None:
    await asyncio.to_thread(_refresh_all_sync)


def _sync_business_guest_names_sync() -> int:
    """把 PVE 实时名称同步回业务关联行的展示快照(后台循环调用)。

    原先在 GET /businesses/{id} 的 _guest_server_items 里顺手改名并 commit——
    读接口带写副作用且嵌在轮询路径上。名称落库后 PVE 不可达时仍能说明业务
    依赖了哪台虚机。返回本轮改名条数。
    """
    db = SessionLocal()
    try:
        rows = db.query(BusinessPveGuest).all()
        changed = 0
        for link in rows:
            state = get_connection_state(link.connection_id)
            if not state or not state.get("reachable"):
                continue
            snap = get_guest_snapshot(link.connection_id, link.vmid)
            if not snap or snap.get("guest_type") != link.guest_type:
                continue
            live_name = str(snap.get("name") or "").strip()[:255]
            if live_name and live_name != (link.guest_name or ""):
                link.guest_name = live_name
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


async def run_pve_guest_status_loop():
    logger.info(
        "PVE guest status collector started (interval=%ds)", PVE_STATUS_INTERVAL
    )
    while True:
        try:
            await refresh_all()
            try:
                await asyncio.to_thread(_sync_business_guest_names_sync)
            except Exception:
                logger.exception("Business guest name sync failed")
            # 虚拟机指标跟着 30s 快照周期评估，不等 60s 指标周期，
            # 否则触发时机总比 sustain 预设慢一个周期。设备条目为空:
            # 无显式范围的设备规则本轮跳过，与指标周期不重复评估
            # (evaluate 入口已加锁串行)。
            from app.services.alerts import (
                evaluate_alerts,
                evaluate_pve_guest_host_status,
            )

            await asyncio.to_thread(evaluate_alerts, {})
            # 虚机主机状态告警同样跟着 30s 快照周期评估(不再等 60s 指标周期):
            # 虚机真实停机后,离线告警最多迟到 sustain+30s,而不是 2~3 个周期。
            await asyncio.to_thread(evaluate_pve_guest_host_status)
        except Exception:
            logger.exception("PVE guest status cycle failed")
        await asyncio.sleep(PVE_STATUS_INTERVAL)
