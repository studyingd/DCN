"""运行数据保留期清理(agent_runs / automation_jobs / inspection_records)。

三张表与 alert_events 不同:它们是**审计/留档**性质的数据(诊断步骤、
任务执行明细、巡检历史),默认**不清理**——0 天 = 与现状完全一致。
启用后只删终态数据(running/pending 永不删),复刻 cleanup_old_alert_events
的成熟模式:每小时由 main.py 维护循环幂等调用,异常只记日志绝不外抛。

删除依靠既有 ORM/DB 级联带走子表:
  * automation_jobs → targets → steps(cascade delete-orphan + FK CASCADE);
  * inspection_records → item_results(同);
  * agent_runs 自包含(steps_json/report 在行内)。

引用防护:alert_events.agent_run_id 是普通列(无 FK)。agent_runs 的清理
跳过仍被任何告警事件引用的行——保证归因详情链接不悬空,也天然满足
「agent_runs 保留期 ≥ 告警事件保留期」的约束,无需用户对齐两个配置。
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import (
    AGENT_RUN_RETENTION_DAYS,
    AUTOMATION_JOB_RETENTION_DAYS,
    INSPECTION_RETENTION_DAYS,
)
from app.database import SessionLocal
from app.models.agent_run import AgentRun
from app.models.alert import AlertEvent
from app.models.automation import AutomationJob
from app.models.inspection import InspectionRecord

logger = logging.getLogger(__name__)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def cleanup_old_agent_runs(days: int = AGENT_RUN_RETENTION_DAYS) -> int:
    """删除超过保留期的终态 Agent 诊断记录,返回删除条数。

    days <= 0 时直接返回(默认关闭)。running 状态永不删;仍被
    alert_events.agent_run_id 引用的行跳过(防归因详情悬空)。
    """
    if days <= 0:
        return 0
    db = SessionLocal()
    try:
        referenced = select(AlertEvent.agent_run_id).where(
            AlertEvent.agent_run_id.isnot(None)
        )
        deleted = (
            db.execute(
                delete(AgentRun)
                .where(
                    AgentRun.status.in_(("completed", "failed")),
                    AgentRun.created_at < _cutoff(days),
                    AgentRun.id.not_in(referenced),
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            or 0
        )
        db.commit()
        if deleted:
            logger.info("Cleaned up %d agent runs older than %d days", deleted, days)
        return deleted
    except Exception:
        db.rollback()
        logger.exception("Failed to clean up old agent runs")
        return 0
    finally:
        db.close()


def cleanup_old_automation_jobs(days: int = AUTOMATION_JOB_RETENTION_DAYS) -> int:
    """删除超过保留期的终态自动化任务(targets/steps 级联带走)。"""
    if days <= 0:
        return 0
    db = SessionLocal()
    try:
        deleted = (
            db.execute(
                delete(AutomationJob)
                .where(
                    AutomationJob.status.in_(("completed", "partial", "failed")),
                    AutomationJob.finished_at.isnot(None),
                    AutomationJob.finished_at < _cutoff(days),
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            or 0
        )
        db.commit()
        if deleted:
            logger.info(
                "Cleaned up %d automation jobs older than %d days", deleted, days
            )
        return deleted
    except Exception:
        db.rollback()
        logger.exception("Failed to clean up old automation jobs")
        return 0
    finally:
        db.close()


def cleanup_old_inspection_records(days: int = INSPECTION_RETENTION_DAYS) -> int:
    """删除超过保留期的巡检留档(item_results 级联带走,含虚机记录)。"""
    if days <= 0:
        return 0
    db = SessionLocal()
    try:
        # 巡检记录只有终态(running 只存在于执行瞬间,进程重启即被收敛),
        # 但防御性排除 running 与缺失 started_at 的行,口径与另两张表一致。
        deleted = (
            db.execute(
                delete(InspectionRecord)
                .where(
                    InspectionRecord.status != "running",
                    InspectionRecord.started_at.isnot(None),
                    InspectionRecord.started_at < _cutoff(days),
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            or 0
        )
        db.commit()
        if deleted:
            logger.info(
                "Cleaned up %d inspection records older than %d days", deleted, days
            )
        return deleted
    except Exception:
        db.rollback()
        logger.exception("Failed to clean up old inspection records")
        return 0
    finally:
        db.close()
