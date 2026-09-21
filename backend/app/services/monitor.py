"""
Background device monitor service.

Periodically checks device reachability via TCP connect to management ports
(Windows: WinRM then RDP; Linux/network: SSH then RDP) and updates the device
status in the database.
"""

import asyncio
import logging

from app.config import MONITOR_CONCURRENCY, MONITOR_INTERVAL, MONITOR_TIMEOUT
from app.database import SessionLocal
from app.models.device import Device, is_windows_os

logger = logging.getLogger(__name__)

_latest_statuses: dict[int, str] = {}
# 连续探测失败计数:单次超时(网络抖动)不立即翻 offline,连续 2 次才翻,
# 避免指示灯闪烁。恢复 online 单次成功即生效。
_fail_counts: dict[int, int] = {}
_scan_lock = asyncio.Lock()

# 连续失败几次才确认为离线(第 1 次失败保持旧状态)
OFFLINE_CONFIRM_SCANS = 2


def get_latest_statuses() -> dict[int, str]:
    return dict(_latest_statuses)


async def _check_tcp_port(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except ConnectionRefusedError:
        # Host reachable, port closed — still counts as online
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _port_open(host: str, port: int, timeout: float) -> bool:
    """True only when the TCP handshake actually completes (SYN-ACK received).

    Unlike _check_tcp_port, a ConnectionRefused (RST) counts as NOT open. This
    distinguishes a genuinely-open port from a closed/filtered one.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _is_real_host(host: str, timeout: float) -> bool:
    """Detect a SYN-answering firewall / NAT / VPN that completes the TCP
    handshake on EVERY port with no real service behind it.

    A powered-off host sitting behind such a device still looks 'open' to a
    plain TCP-connect check, so the monitor would falsely report it online.
    A real host REFUSES (RST) or filters arbitrary high ports; if both of two
    sentinel ports accept a connection, the IP is being intercepted and there
    is no real host — return False.

    不适用于云服务器：见 _host_guard_applies。
    """
    sentinels = (34567, 54321)
    probe_timeout = min(timeout, 1.0)
    accepts = 0
    for p in sentinels:
        if await _port_open(host, p, probe_timeout):
            accepts += 1
    return accepts < 2


def _host_guard_applies(device_type: str) -> bool:
    """是否对该设备执行 _is_real_host 哨兵端口守卫。

    云服务器一律跳过：它们的 SLB / NAT / 安全组本来就会对任意端口应答握手，
    守卫会把健康的云服务器误判为 offline，连带自动化运维的
    deviceSelectable(status === 'online') 把它变成不可选。
    物理服务器/台式主机保留守卫，因为其假在线风险是真实的。
    """
    return (device_type or "").strip().lower() != "cloud_server"


def _load_devices(db) -> list[tuple[int, str, int, int, int, str, str, str]]:
    rows = db.query(
        Device.id,
        Device.ip_address,
        Device.ssh_port,
        Device.rdp_port,
        Device.winrm_port,
        Device.os_system,
        Device.status,
        Device.type,
    ).all()
    return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]


def _update_device_statuses(db, updates: dict[int, str]) -> None:
    devices = db.query(Device).filter(Device.id.in_(updates.keys())).all()
    for device in devices:
        new_status = updates[device.id]
        if device:
            device.status = new_status
    db.commit()


async def _run_scan_unlocked() -> dict[int, str]:
    global _latest_statuses
    loop = asyncio.get_running_loop()

    # Load devices from DB (sync)
    db = SessionLocal()
    try:
        devices = await loop.run_in_executor(None, _load_devices, db)
    finally:
        db.close()

    # Check reachability concurrently
    sem = asyncio.Semaphore(MONITOR_CONCURRENCY)
    results: dict[int, str] = {}

    async def check_one(
        device_id: int,
        ip: str,
        ssh_port: int,
        rdp_port: int,
        winrm_port: int,
        os_system: str | None,
        current: str,
        device_type: str,
    ):
        if not ip:
            results[device_id] = "offline"
            return
        async with sem:
            # Windows 设备不装 SSH——管理通道是 WinRM,主探 5985,再退 RDP。
            if is_windows_os(os_system):
                primary = winrm_port or 5985
            else:
                primary = ssh_port
            online = await _check_tcp_port(ip, primary, MONITOR_TIMEOUT)
            if not online:
                online = await _check_tcp_port(ip, rdp_port, MONITOR_TIMEOUT)
            if not online:
                results[device_id] = "offline"
                return
            # Guard: a SYN-answering firewall/VPN/NAT can make a powered-off
            # host look 'open' on every port. Confirm a real host is actually
            # present before trusting a port connect as "online".
            # 端口探通之后才跑哨兵(2 次额外 TCP 连接):端口本就不通的设备
            # 必然 offline,先探哨兵只会白付最长 2×1s 的超时,拖慢整轮扫描。
            # 云服务器豁免，理由见 _host_guard_applies。
            if _host_guard_applies(device_type) and not await _is_real_host(
                ip, MONITOR_TIMEOUT
            ):
                results[device_id] = "offline"
                return
            results[device_id] = "online"

    await asyncio.gather(
        *[
            check_one(did, ip, ssh, rdp, winrm, os_sys, cur, dtype)
            for did, ip, ssh, rdp, winrm, os_sys, cur, dtype in devices
        ]
    )

    # Persist changes (only devices whose status actually changed)
    changed: dict[int, str] = {}
    for did, new_status in results.items():
        if new_status not in ("online", "offline"):
            continue
        if new_status == "offline":
            fails = _fail_counts.get(did, 0) + 1
            _fail_counts[did] = fails
            if fails < OFFLINE_CONFIRM_SCANS:
                # 首次失败:沿用上一轮结果,不落库不翻灯
                results[did] = _latest_statuses.get(did, "offline")
                continue
        else:
            _fail_counts.pop(did, None)
        old = _latest_statuses.get(did)
        if old != results[did]:
            changed[did] = results[did]

    if changed:
        db2 = SessionLocal()
        try:
            await loop.run_in_executor(None, _update_device_statuses, db2, changed)
        finally:
            db2.close()

    _latest_statuses = results

    # 已删除设备的残留计数清理(微量,但常年运行会无界增长)
    alive = {did for did in results}
    for did in [k for k in _fail_counts if k not in alive]:
        _fail_counts.pop(did, None)

    return results


async def _run_scan() -> dict[int, str]:
    """Serialize manual and periodic scans to avoid duplicate probes/writes."""
    async with _scan_lock:
        return await _run_scan_unlocked()


async def run_monitor_loop():
    logger.info(
        "Device monitor started (interval=%ds, timeout=%ds)",
        MONITOR_INTERVAL,
        MONITOR_TIMEOUT,
    )
    while True:
        try:
            await _run_scan()
        except Exception:
            logger.exception("Monitor scan cycle failed")

        await asyncio.sleep(MONITOR_INTERVAL)


async def trigger_scan() -> dict[int, str]:
    return await _run_scan()
