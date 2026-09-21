"""PVE 虚拟机文件系统使用率的进程内缓存(供磁盘告警评估)。

虚拟机的 cpu/mem 告警数据来自 pve_guest_status 快照(PVE API,零成本);磁盘
使用率 PVE 侧没有——只能进 guest 内部取:QGA ``get-fsinfo`` 优先(不需要 guest
网络凭据),QGA 不可用时回退运维接入绑定的 SSH/WinRM。这两条路径都要真实
调用(PVE API + QGA ping / SSH 握手),比读内存快照贵得多,所以:

* 由独立后台循环低频采集(默认 300s 一轮,``GUEST_FS_METRICS_INTERVAL``),
  评估器只读内存缓存,不会把 SSH/QGA 压到 30s 评估周期上;
* 只在存在**启用中的 disk_max_pct 告警规则**时才干活,没有磁盘规则零开销;
* 与 pve_guest_status 同一模式:仅 leader 实例刷新,多副本部署时其余实例
  读到空缓存(磁盘值缺失 → 评估跳过,事件不会被误恢复)。

告警口径与设备侧 disk_max_pct 一致:所有可用文件系统行里**使用率最大的那个**
(设备侧 df/Win32_LogicalDisk 同为"取最大")。采集失败保留上一轮值并记录
error,评估侧按"数据缺失不收敛"处理(宁可晚一轮,不误关告警)。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.config import GUEST_FS_METRICS_INTERVAL
from app.database import SessionLocal
from app.models.pve_guest_binding import PveGuestBinding
from app.services.containers_collector import decode_pve_target_id
from app.services.crypto import decrypt
from app.services.pve import build_client, guest_agent_fsinfo_summary
from app.services.pve_guest_metrics import collect_guest_filesystems
from app.services.pve_guest_status import get_guests_by_connection

logger = logging.getLogger(__name__)

# 单轮采集的目标并发数(与 AUTOMATION_MAX_CONCURRENT_TARGETS 同量级):
# QGA 命令 4s 超时 ×SSH/WinRM 兑底十几秒,串行 50 台最坏十几分钟;
# 并发 4 把最坏路径压到 1/4,又不至于把 PVE/SSH 打成风暴。
_COLLECT_CONCURRENCY = 4

# (connection_id, vmid) -> {"available", "sampled_at", "disk_max_pct", "error"}
_cache: dict[tuple[int, int], dict] = {}
_lock = threading.Lock()


def get_guest_disk_entry(connection_id: int, vmid: int) -> dict | None:
    """单个 guest 的磁盘使用率缓存条目;未采集返回 None。"""
    with _lock:
        item = _cache.get((connection_id, vmid))
        return dict(item) if item else None


def _store(connection_id: int, vmid: int, entry: dict) -> None:
    with _lock:
        _cache[(connection_id, vmid)] = entry


def _disk_max_pct(disks: list[dict]) -> float | None:
    """所有可用文件系统行里使用率最大的那个(设备侧同口径)。"""
    best: float | None = None
    for disk in disks:
        total = int(disk.get("total_bytes") or 0)
        used = int(disk.get("used_bytes") or 0)
        if total <= 0:
            continue
        pct = round(used / total * 100.0, 1)
        if best is None or pct > best:
            best = pct
    return best


def _wanted_guest_keys(db: Session) -> tuple[set[tuple[int, int]], bool]:
    """扫描启用中的 disk_max_pct 规则,返回 (显式目标, 是否需要全量)。

    显式目标含负数 id 时按 id 解码;目标为空(=全部对象)时需要给所有可采集的
    running guest 都建立缓存。
    """
    from app.models.alert import AlertRule

    rules = (
        db.query(AlertRule)
        .filter(AlertRule.metric == "disk_max_pct", AlertRule.enabled.is_(True))
        .all()
    )
    explicit: set[tuple[int, int]] = set()
    collect_all = False
    for rule in rules:
        targets = rule.target_device_ids or []
        if not targets:
            collect_all = True
            continue
        for tid in targets:
            decoded = decode_pve_target_id(tid)
            if decoded:
                explicit.add(decoded)
    return explicit, collect_all


def refresh_all(db: Session | None = None) -> int:
    """刷新一轮虚拟机文件系统使用率缓存,返回本轮成功采集的 guest 数。"""
    from app.models.pve_connection import PveConnection

    own_session = db is None
    db = db or SessionLocal()
    collected = 0
    try:
        explicit, collect_all = _wanted_guest_keys(db)
        conns = db.query(PveConnection).filter(PveConnection.enabled == 1).all()
        # 先清理已销毁虚机/停用连接的缓存条目:判定依据是 pve_guest_status 快照
        # 里的存在性(不是"是否被规则需要"),放在规则闸门早退之前,没有磁盘规则
        # 的周期也照样清理,长跑进程不至于缓慢泄漏。
        existing: set[tuple[int, int]] = set()
        for conn in conns:
            for vmid in get_guests_by_connection(conn.id):
                existing.add((conn.id, vmid))
        with _lock:
            for key in [k for k in _cache if k not in existing]:
                _cache.pop(key, None)
        if not explicit and not collect_all:
            return 0
        # 收集本轮要采集的目标(顺序确定:按 vmid 升序,结果回存顺序与串行版一致)。
        work: list[tuple] = []
        for conn in conns:
            guests = get_guests_by_connection(conn.id)
            if not guests:
                continue
            bindings = {
                b.vmid: b
                for b in db.query(PveGuestBinding)
                .filter(PveGuestBinding.connection_id == conn.id)
                .all()
            }
            for vmid, guest in sorted(guests.items()):
                key = (conn.id, vmid)
                binding = bindings.get(vmid)
                if key not in explicit and not (
                    collect_all and (binding or str(guest.get("status")) == "running")
                ):
                    continue
                if str(guest.get("status") or "") != "running":
                    # 停机 guest 采不到 fs;留空让评估侧按数据缺失跳过
                    continue
                work.append((conn, guest, binding))

        def _run(item: tuple) -> dict:
            conn, guest, binding = item
            try:
                # build_client 进程内缓存复用,线程安全(内部有锁)。
                client = build_client(conn)
                return _collect_one(client, conn, guest, binding)
            except Exception as exc:
                return {
                    "available": False,
                    "sampled_at": time.time(),
                    "disk_max_pct": None,
                    "error": str(exc)[:200],
                }

        if work:
            with ThreadPoolExecutor(max_workers=_COLLECT_CONCURRENCY) as pool:
                # pool.map 保序:回存顺序与串行版完全一致。
                for (conn, guest, _binding), entry in zip(
                    work, pool.map(_run, work), strict=True
                ):
                    _store(conn.id, int(guest.get("vmid")), entry)
                    if entry.get("available"):
                        collected += 1
        return collected
    finally:
        if own_session:
            db.close()


def _collect_one(client, conn, guest: dict, binding) -> dict:
    """单个 guest:QGA fsinfo 优先(不需要 guest 网络凭据),运维接入兜底。

    只调 get-fsinfo 一条 QGA 命令(磁盘循环不消费 interfaces/osinfo,见
    ``guest_agent_fsinfo_summary``);QGA 地址缓存由专门的 IP 刷新循环
    维护,这里不再顺手记录。
    """
    node = str(guest.get("node") or "")
    gtype = str(guest.get("guest_type") or "qemu")
    vmid = int(guest.get("vmid"))
    qga = guest_agent_fsinfo_summary(client, node, gtype, vmid)

    binding_ip = (binding.ip_address or None) if binding else None
    binding_os = (binding.os_system or None) if binding else None
    username = (binding.username or None) if binding else None
    password = (
        decrypt(binding.password_enc) if binding and binding.password_enc else None
    )
    ssh_key = decrypt(binding.ssh_key_enc) if binding and binding.ssh_key_enc else None

    result = collect_guest_filesystems(
        qga,
        ip_address=(qga.get("qga_ip_address") if qga.get("qga_available") else None)
        or binding_ip,
        os_system=(qga.get("qga_os_system") if qga.get("qga_available") else None)
        or binding_os,
        username=username,
        password=password,
        ssh_key=ssh_key,
        ssh_port=(binding.ssh_port or 22) if binding else 22,
        winrm_port=(binding.winrm_port or 5985) if binding else 5985,
        ssh_host_key=(binding.ssh_host_key or None) if binding else None,
    )
    disks = result.get("filesystem_disks") or []
    value = _disk_max_pct(disks)
    return {
        "available": bool(result.get("filesystem_available")) and value is not None,
        "sampled_at": time.time(),
        "disk_max_pct": value,
        "error": result.get("filesystem_error"),
        "source": result.get("filesystem_source"),
    }


def _refresh_all_sync() -> None:
    try:
        refresh_all()
    except Exception:
        logger.exception("Guest filesystem metrics cycle failed")


async def run_guest_fs_metrics_loop():
    logger.info(
        "Guest filesystem metrics collector started (interval=%ds)",
        GUEST_FS_METRICS_INTERVAL,
    )
    while True:
        await asyncio.to_thread(_refresh_all_sync)
        # 虚拟机磁盘告警评估跟着 30s 快照周期跑,但磁盘值本身 5 分钟才变一轮;
        # 采集一轮后同步评估一次,避免新告警最多迟到 interval+30s。
        try:
            from app.services.alerts import evaluate_alerts

            await asyncio.to_thread(evaluate_alerts, {})
        except Exception:
            logger.exception("Disk alert evaluation after fs collection failed")
        await asyncio.sleep(GUEST_FS_METRICS_INTERVAL)
