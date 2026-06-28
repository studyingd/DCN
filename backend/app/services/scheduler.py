"""Background scheduler for scheduled script tasks."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.device import Device
from app.models.scheduled_task import ScheduledTask
from app.services.cron import next_cron_time
from app.utils import utcnow
from app.validators import validate_script_command

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 30
MAX_CONCURRENT_SSH = 10


def _execute_task(task: ScheduledTask):
    """Execute a scheduled task on its target devices (runs in thread pool)."""
    db = SessionLocal()
    try:
        devices = db.query(Device).filter(Device.id.in_(task.device_ids)).all()
        if not devices:
            logger.warning("Scheduled task %d: no devices found", task.id)
            return

        # Re-check the command at run time (policy may have tightened, or the
        # task predates the blocklist). Skip + audit if it is now blocked.
        try:
            validate_script_command(task.command)
        except ValueError:
            logger.warning(
                "Scheduled task %d command blocked by policy: %s", task.id, task.command
            )
            db.add(
                AuditLog(
                    user_id=task.created_by,
                    event_type="command_blocked",
                    command=f"[定时:{task.name}] {task.command}",
                    device_name=",".join(d.name for d in devices),
                    blocked=len(devices),
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            return

        from app.routers.scripts import (
            ScriptExecuteRequest,
            _run_on_device,
        )

        body = ScriptExecuteRequest(
            device_ids=task.device_ids,
            command=task.command,
            timeout=task.timeout,
            credential_id=task.credential_id,
        )

        results = []
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SSH) as pool:
            futures = {
                pool.submit(_run_on_device, d, task.command, task.timeout, body): d.id
                for d in devices
            }
            for future in as_completed(futures):
                results.append(future.result())

        results.sort(key=lambda r: r.device_id)
        succeeded = sum(1 for r in results if r.success)

        # Resolve creator username
        creator_name = None
        if task.created_by:
            from app.models.user import User

            user = db.query(User).filter(User.id == task.created_by).first()
            if user:
                creator_name = user.username

        # Audit log
        log = AuditLog(
            user_id=task.created_by,
            username=creator_name,
            event_type="script_executed",
            command=f"[定时:{task.name}] {task.command}",
            device_name=",".join(d.name for d in devices),
            device_ip=",".join(d.ip_address or "" for d in devices),
            blocked=len(results) - succeeded,
            created_at=utcnow(),
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        sid = f"sched_{task.id}_{log.id}"
        log.session_id = sid
        db.commit()

        detail = AuditLog(
            session_id=sid,
            user_id=task.created_by,
            username=creator_name,
            event_type="script_result",
            command=json.dumps(
                [
                    {
                        "device_name": r.device_name,
                        "ip": r.ip_address,
                        "success": r.success,
                        "exit_code": r.exit_code,
                        "stdout": (r.stdout or "")[:2000],
                        "stderr": (r.stderr or "")[:500],
                        "error": (r.error or "")[:500],
                    }
                    for r in results
                ],
                ensure_ascii=False,
            ),
            device_name=",".join(d.name for d in devices),
            blocked=succeeded,
            created_at=datetime.now(timezone.utc),
        )
        db.add(detail)
        db.commit()

        logger.info(
            "Scheduled task %d executed: %d/%d succeeded",
            task.id,
            succeeded,
            len(results),
        )
    except Exception:
        logger.exception("Error executing scheduled task %d", task.id)
    finally:
        db.close()


async def run_scheduler_loop():
    """Main scheduler loop — checks for due tasks every 30 seconds."""
    logger.info("Scheduled task scheduler started (interval=%ds)", SCHEDULER_INTERVAL)
    loop = asyncio.get_running_loop()

    while True:
        try:
            await _check_and_run_due_tasks(loop)
        except Exception:
            logger.exception("Scheduler check cycle failed")

        await asyncio.sleep(SCHEDULER_INTERVAL)


async def _check_and_run_due_tasks(loop):
    db = SessionLocal()
    try:
        now = utcnow()
        due_tasks = (
            db.query(ScheduledTask)
            .filter(
                ScheduledTask.status.in_(["pending", "active"]),
                ScheduledTask.next_run_at != None,  # noqa: E711
                ScheduledTask.next_run_at <= now,
            )
            .all()
        )

        for task in due_tasks:
            logger.info("Executing scheduled task %d: %s", task.id, task.name)

            task.last_run_at = now
            if task.schedule_type == "once":
                task.status = "completed"
                task.next_run_at = None
            else:
                try:
                    task.next_run_at = next_cron_time(task.cron_expression, now)
                except ValueError:
                    logger.error(
                        "Invalid cron for task %d: %s", task.id, task.cron_expression
                    )
                    task.status = "disabled"
                    task.next_run_at = None
            db.commit()

            await loop.run_in_executor(None, _execute_task, task)
    except Exception:
        logger.exception("Error in scheduler check cycle")
    finally:
        db.close()
