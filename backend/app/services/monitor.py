"""
Background device monitor service.

Periodically checks device reachability via TCP connect to SSH/RDP ports
and updates the device status in the database.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.config import MONITOR_CONCURRENCY, MONITOR_INTERVAL, MONITOR_TIMEOUT
from app.database import SessionLocal
from app.models.device import Device

logger = logging.getLogger(__name__)

_latest_statuses: dict[int, str] = {}


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
    """
    sentinels = (34567, 54321)
    probe_timeout = min(timeout, 1.0)
    accepts = 0
    for p in sentinels:
        if await _port_open(host, p, probe_timeout):
            accepts += 1
    return accepts < 2


def _load_devices(db) -> list[tuple[int, str, int, int, str]]:
    rows = db.query(
        Device.id,
        Device.ip_address,
        Device.ssh_port,
        Device.rdp_port,
        Device.status,
    ).all()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def _update_device_statuses(db, updates: dict[int, str]) -> None:
    for device_id, new_status in updates.items():
        device = db.query(Device).filter(Device.id == device_id).first()
        if device and device.status != "maintenance":
            device.status = new_status
    db.commit()


async def _run_scan() -> dict[int, str]:
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
        device_id: int, ip: str, ssh_port: int, rdp_port: int, current: str
    ):
        if current == "maintenance":
            results[device_id] = "maintenance"
            return
        if not ip:
            results[device_id] = "offline"
            return
        async with sem:
            # Guard: a SYN-answering firewall/VPN/NAT can make a powered-off
            # host look 'open' on every port. Confirm a real host is actually
            # present before trusting a port connect as "online".
            if not await _is_real_host(ip, MONITOR_TIMEOUT):
                results[device_id] = "offline"
                return
            online = await _check_tcp_port(ip, ssh_port, MONITOR_TIMEOUT)
            if not online:
                online = await _check_tcp_port(ip, rdp_port, MONITOR_TIMEOUT)
            results[device_id] = "online" if online else "offline"

    await asyncio.gather(
        *[check_one(did, ip, ssh, rdp, cur) for did, ip, ssh, rdp, cur in devices]
    )

    # Persist changes (only devices whose status actually changed)
    changed: dict[int, str] = {}
    for did, new_status in results.items():
        if new_status not in ("online", "offline"):
            continue
        old = _latest_statuses.get(did)
        if old != new_status:
            changed[did] = new_status

    if changed:
        db2 = SessionLocal()
        try:
            await loop.run_in_executor(None, _update_device_statuses, db2, changed)
        finally:
            db2.close()

    _latest_statuses = results

    return results


def _write_daily_snapshot(statuses: dict[int, str]) -> None:
    """Write or update a daily stats snapshot row for today's date."""
    from app.models.audit_log import AuditLog
    from app.models.connection import Connection
    from app.models.daily_stats import DailyStatsSnapshot

    today_str = datetime.now(timezone.utc).date().isoformat()
    db = SessionLocal()
    try:
        snapshot = (
            db.query(DailyStatsSnapshot)
            .filter(DailyStatsSnapshot.snapshot_date == today_str)
            .first()
        )

        # Compute counts from the live status cache
        device_total = len(statuses)
        device_online = sum(1 for s in statuses.values() if s == "online")
        device_offline = sum(1 for s in statuses.values() if s == "offline")
        device_maintenance = sum(1 for s in statuses.values() if s == "maintenance")

        # DB-based counts
        connection_count = db.query(func.count(Connection.id)).scalar() or 0

        # Audit-based counts for today
        today_start = datetime.combine(
            datetime.now(timezone.utc).date(), datetime.min.time(), tzinfo=timezone.utc
        )
        session_count = (
            db.query(func.count(AuditLog.id))
            .filter(
                AuditLog.event_type.in_(["session_start", "session_end"]),
                AuditLog.created_at >= today_start,
            )
            .scalar()
            or 0
        )
        script_execution_count = (
            db.query(func.count(AuditLog.id))
            .filter(
                AuditLog.event_type == "command_executed",
                AuditLog.created_at >= today_start,
            )
            .scalar()
            or 0
        )
        login_count = (
            db.query(func.count(AuditLog.id))
            .filter(
                AuditLog.event_type == "system_login",
                AuditLog.created_at >= today_start,
            )
            .scalar()
            or 0
        )

        def _apply(snap: DailyStatsSnapshot) -> None:
            snap.device_total = device_total
            snap.device_online = device_online
            snap.device_offline = device_offline
            snap.device_maintenance = device_maintenance
            snap.connection_count = connection_count
            snap.session_count = session_count
            snap.script_execution_count = script_execution_count
            snap.login_count = login_count

        if snapshot:
            # Update existing row
            _apply(snapshot)
            db.commit()
        else:
            # Create new row for today. Two monitor cycles racing at day-rollover
            # can both see no row and both INSERT; the UNIQUE(snapshot_date)
            # constraint makes the loser raise IntegrityError — recover by
            # re-fetching the winner's row and updating it.
            snapshot = DailyStatsSnapshot(
                snapshot_date=today_str,
                device_total=device_total,
                device_online=device_online,
                device_offline=device_offline,
                device_maintenance=device_maintenance,
                connection_count=connection_count,
                session_count=session_count,
                script_execution_count=script_execution_count,
                login_count=login_count,
            )
            db.add(snapshot)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = (
                    db.query(DailyStatsSnapshot)
                    .filter(DailyStatsSnapshot.snapshot_date == today_str)
                    .first()
                )
                if existing is not None:
                    _apply(existing)
                    db.commit()
    except Exception:
        logger.exception("Failed to write daily stats snapshot")
        db.rollback()
    finally:
        db.close()


async def run_monitor_loop():
    import time

    from app.services.retention import purge_expired_data

    logger.info(
        "Device monitor started (interval=%ds, timeout=%ds)",
        MONITOR_INTERVAL,
        MONITOR_TIMEOUT,
    )
    last_retention = 0
    while True:
        try:
            scan_results = await _run_scan()
            _write_daily_snapshot(scan_results)
        except Exception:
            logger.exception("Monitor scan cycle failed")

        # Run retention purge once every 24 hours
        now = time.time()
        if now - last_retention >= 86400:
            try:
                purge_expired_data()
            except Exception:
                logger.exception("Retention purge failed")
            last_retention = now

        await asyncio.sleep(MONITOR_INTERVAL)


async def trigger_scan() -> dict[int, str]:
    return await _run_scan()
