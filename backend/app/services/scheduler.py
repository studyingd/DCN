"""Background scheduler for unified automation schedules.

旧 ScheduledTask(脚本专用定时任务)链路已删除:它与新 AutomationSchedule
并存造成双入口分裂——旧计划的执行结果不落统一任务表(无 steps 审计)、
不支持 PVE 虚拟机、没有 Webhook 巡检报告。合并后定时计划唯一入口是
/api/automation/schedules,到期统一 create_job_record 走自动化执行引擎。

paused 语义(用户主动暂停,随时恢复)与 disabled(授权失效系统停用,
需排查后重建)严格分开:到期扫描只认 active,paused 的 next_run_at
保持不动,恢复时按周期继续。
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.automation import AutomationSchedule
from app.models.user import User
from app.services.automation import create_job_record, start_automation_job
from app.services.cron import next_cron_time
from app.services.permissions import user_can_access_device, user_has_permission

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 30


async def run_scheduler_loop():
    """Main scheduler loop — checks for due schedules every 30 seconds."""
    logger.info("Automation scheduler started (interval=%ds)", SCHEDULER_INTERVAL)
    loop = asyncio.get_running_loop()

    while True:
        try:
            await _check_and_run_due_automation_schedules(loop)
        except Exception:
            logger.exception("Scheduler check cycle failed")

        await asyncio.sleep(SCHEDULER_INTERVAL)


async def _check_and_run_due_automation_schedules(loop):
    """Create unified jobs for due automation schedules.

    目标支持设备(正 id)与 PVE 虚拟机(合成负数 target_id)混合:授权按类型
    分流(设备→device ACL;虚机→pve 权限+vmid ACL,power 任务要求 manage),
    与 routers/automation.create_schedule 同口径;到期时把负数 id 解回
    (connection_id, vmid) 拼进 create_job_record 的 pve_targets。
    授权失效/虚机平台删除时停用计划并留日志,行为与设备目标失权时一致。
    paused(用户暂停)不进到期扫描,恢复后按既有周期继续。
    """
    from app.models.pve_connection import PveConnection
    from app.models.pve_guest_binding import PveGuestBinding
    from app.services.containers_collector import decode_pve_target_id
    from app.services.permissions import user_can_access_pve, user_can_access_pve_vmid

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        schedules = (
            db.query(AutomationSchedule)
            .filter(
                AutomationSchedule.status == "active",
                AutomationSchedule.next_run_at != None,  # noqa: E711
                AutomationSchedule.next_run_at <= now,
            )
            .all()
        )
        for schedule in schedules:
            owner = db.query(User).filter(User.id == schedule.created_by).first()
            permission = {
                "agent": "device:remote",
                "inspection": "automation:manage",
                "script": "automation:manage",
                "power": "automation:manage",
            }.get(schedule.job_type)
            authorized = bool(
                owner
                and owner.is_active
                and permission
                and user_has_permission(owner, permission, db)
            )
            device_ids: list[int] = []
            pve_targets: list[dict] = []
            if authorized:
                for tid in schedule.target_ids or []:
                    decoded = decode_pve_target_id(tid)
                    if decoded:
                        connection_id, vmid = decoded
                        conn = (
                            db.query(PveConnection)
                            .filter(
                                PveConnection.id == connection_id,
                                PveConnection.enabled == 1,
                            )
                            .first()
                        )
                        binding = (
                            db.query(PveGuestBinding)
                            .filter_by(
                                connection_id=connection_id, vmid=vmid, enabled=1
                            )
                            .first()
                        )
                        if not user_can_access_pve(
                            owner, db, manage=schedule.job_type == "power"
                        ) or not user_can_access_pve_vmid(
                            owner, db, connection_id, vmid
                        ):
                            authorized = False
                            break
                        if conn is None:
                            authorized = False
                            break
                        pve_targets.append(
                            {
                                "connection_id": connection_id,
                                "vmid": vmid,
                                "guest_type": (
                                    binding.guest_type if binding else "qemu"
                                ),
                                "name": f"[{conn.name}] {(binding and binding.ip_address) or f'VM {vmid}'}",
                            }
                        )
                    elif tid > 0:
                        if not user_can_access_device(owner, tid, db):
                            authorized = False
                            break
                        device_ids.append(tid)
            if not authorized:
                schedule.status = "disabled"
                schedule.next_run_at = None
                db.commit()
                logger.warning(
                    "Automation schedule %d disabled after permission check",
                    schedule.id,
                )
                continue
            job = create_job_record(
                db,
                name=schedule.name,
                job_type=schedule.job_type,
                device_ids=device_ids,
                pve_targets=pve_targets,
                config=dict(schedule.config_json or {}),
                user_id=schedule.created_by,
                user_name=schedule.created_by_name,
                trigger_type="scheduled",
            )
            schedule.last_run_at = now
            if schedule.schedule_type == "once":
                schedule.status = "completed"
                schedule.next_run_at = None
            else:
                try:
                    schedule.next_run_at = next_cron_time(schedule.cron_expression, now)
                except ValueError:
                    schedule.status = "disabled"
                    schedule.next_run_at = None
            db.commit()
            start_automation_job(job.id)
    except Exception:
        logger.exception("Error executing unified automation schedules")
    finally:
        db.close()
