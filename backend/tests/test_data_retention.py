"""运行数据保留期清理(P0-3)的行为测试。

钉住四件事:
  1. days<=0(默认)时三函数零删除、零查询——「装好水龙头但默认不开」;
  2. 只删终态:running/pending 的行永不删;
  3. agent_runs 跳过仍被 alert_events.agent_run_id 引用的行(防归因悬空);
  4. 级联:删 job 自动带走 targets/steps,删 record 带走 items;幂等。

数据卫生约定:只删本运行创建的行(按 name 前缀过滤),不动用户真实数据。
"""

from datetime import datetime, timedelta, timezone

from app.database import Base, SessionLocal, engine
from app.models.agent_run import AgentRun
from app.models.alert import AlertEvent
from app.models.automation import AutomationJob, AutomationJobStep, AutomationJobTarget
from app.models.device import Device
from app.models.inspection import InspectionRecord
from app.models.inspection_item import InspectionItemResult
from app.models.rack import Rack
from app.models.room import Room
from app.services.data_retention import (
    cleanup_old_agent_runs,
    cleanup_old_automation_jobs,
    cleanup_old_inspection_records,
)

_STAMP = datetime.now(timezone.utc).timestamp()
_NAME = f"retention_{_STAMP}".replace(".", "")
_OLD = datetime.now(timezone.utc) - timedelta(days=400)
_FRESH = datetime.now(timezone.utc) - timedelta(days=1)


def _setup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room = Room(name=f"{_NAME}_room")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"{_NAME}_rack", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"{_NAME}_device",
        type="server",
        ip_address="192.0.2.99",
        status="online",
        os_system="Linux",
    )
    db.add(device)
    db.flush()
    return db, room, rack, device


def _teardown(db, room, rack, device):
    # 只删本运行的行:按名字前缀精确定位,不碰用户数据
    db.query(AgentRun).filter(AgentRun.question.like(f"{_NAME}%")).delete()
    db.query(AlertEvent).filter(AlertEvent.rule_name == _NAME).delete()
    for job in db.query(AutomationJob).filter(AutomationJob.name == _NAME).all():
        db.delete(job)  # ORM 级联带走 targets/steps
    db.query(InspectionRecord).filter(InspectionRecord.device_name == _NAME).delete(
        synchronize_session=False
    )
    db.query(InspectionItemResult).filter(
        InspectionItemResult.command_used == _NAME
    ).delete()
    db.delete(device)
    db.delete(rack)
    db.delete(room)
    db.commit()
    db.close()


def test_zero_days_is_noop():
    """默认 0:三个函数零删除(水龙头装好但不开)。"""
    db, room, rack, device = _setup()
    try:
        db.add(
            AgentRun(
                user_id=None,
                device_id=device.id,
                device_name=_NAME,
                device_ip="192.0.2.99",
                question=f"{_NAME} q",
                status="completed",
                created_at=_OLD,
            )
        )
        db.commit()
        assert cleanup_old_agent_runs(0) == 0
        assert cleanup_old_automation_jobs(0) == 0
        assert cleanup_old_inspection_records(0) == 0
        assert db.query(AgentRun).filter(AgentRun.question == f"{_NAME} q").count() == 1
    finally:
        _teardown(db, room, rack, device)


def test_agent_runs_terminal_state_filter_and_reference_guard():
    """只删终态;仍被 alert_events 引用的行跳过。"""
    db, room, rack, device = _setup()
    try:
        # 四条过期 run:running / completed 未被引用 / completed 被引用 / failed
        ids = {}
        for key, status in [
            ("running", "running"),
            ("done", "completed"),
            ("refd", "completed"),
            ("failed", "failed"),
        ]:
            run = AgentRun(
                user_id=None,
                device_id=device.id,
                device_name=_NAME,
                device_ip="192.0.2.99",
                question=f"{_NAME} {key}",
                status=status,
                created_at=_OLD,
            )
            db.add(run)
            db.flush()
            ids[key] = run.id
        # 一条告警事件引用 "refd" run(模拟归因详情在查)
        db.add(
            AlertEvent(
                rule_id=None,
                rule_name=_NAME,
                device_id=device.id,
                resource_type="device",
                resource_id="1",
                resource_name=_NAME,
                metric="cpu_pct",
                value=99.0,
                threshold=90.0,
                severity="critical",
                status="open",
                message=f"{_NAME} msg",
                first_triggered_at=_FRESH,
                last_seen_at=_FRESH,
                occurrence_count=1,
                notification_status="pending",
                agent_run_id=ids["refd"],
            )
        )
        db.commit()

        deleted = cleanup_old_agent_runs(90)
        assert deleted == 2  # done + failed;running 与被引用的 refd 保留

        remaining = {
            r.question.split()[-1]
            for r in db.query(AgentRun)
            .filter(AgentRun.question.like(f"{_NAME}%"))
            .all()
        }
        assert remaining == {"running", "refd"}
    finally:
        _teardown(db, room, rack, device)


def test_automation_jobs_cascade_and_terminal_filter():
    """终态 job 连 targets/steps 级联删除;running job 不动。"""
    db, room, rack, device = _setup()
    try:
        for status in ("completed", "running"):
            job = AutomationJob(
                name=_NAME,
                job_type="script",
                trigger_type="manual",
                status=status,
                risk_level="high",
                config_json={"command": "x"},
                created_by=None,
                finished_at=_OLD if status == "completed" else None,
            )
            db.add(job)
            db.flush()
            target = AutomationJobTarget(
                job_id=job.id,
                device_id=device.id,
                device_name=_NAME,
                device_ip="192.0.2.99",
                status="completed" if status == "completed" else "running",
            )
            db.add(target)
            db.flush()
            db.add(
                AutomationJobStep(
                    target_id=target.id,
                    step_type="script",
                    step_name="执行脚本",
                    status="completed",
                    command=_NAME,
                )
            )
        db.commit()

        deleted = cleanup_old_automation_jobs(90)
        assert deleted == 1

        jobs = db.query(AutomationJob).filter(AutomationJob.name == _NAME).all()
        assert len(jobs) == 1 and jobs[0].status == "running"
        targets = (
            db.query(AutomationJobTarget)
            .filter(AutomationJobTarget.device_name == _NAME)
            .all()
        )
        assert len(targets) == 1  # running job 的 target 留着
        steps = (
            db.query(AutomationJobStep).filter(AutomationJobStep.command == _NAME).all()
        )
        assert len(steps) == 1  # 终态 job 的 step 被级联带走
    finally:
        _teardown(db, room, rack, device)


def test_inspection_records_cascade():
    """过期巡检留档连同 items 级联删除(含虚机记录 device_id=None)。"""
    db, room, rack, device = _setup()
    try:
        for dev_id, days in [(device.id, 400), (None, 400), (device.id, 1)]:
            rec = InspectionRecord(
                device_id=dev_id,
                device_name=_NAME,
                device_ip="192.0.2.99",
                target_type="linux",
                mode="core",
                status="completed",
                total_items=1,
                started_at=datetime.now(timezone.utc) - timedelta(days=days),
            )
            db.add(rec)
            db.flush()
            db.add(
                InspectionItemResult(
                    record_id=rec.id,
                    item_type="cpu",
                    success=True,
                    value=1.0,
                    unit="%",
                    status="normal",
                    command_used=_NAME,
                )
            )
        db.commit()

        deleted = cleanup_old_inspection_records(90)
        assert deleted == 2  # 两条 400 天的(含虚机),1 天的保留

        recs = (
            db.query(InspectionRecord)
            .filter(InspectionRecord.device_name == _NAME)
            .all()
        )
        assert len(recs) == 1
        items = (
            db.query(InspectionItemResult)
            .filter(InspectionItemResult.command_used == _NAME)
            .all()
        )
        assert len(items) == 1  # 过期 items 级联带走
    finally:
        _teardown(db, room, rack, device)


def test_idempotent_second_run_deletes_nothing():
    """连跑两遍,第二遍零删除。"""
    db, room, rack, device = _setup()
    try:
        run = AgentRun(
            user_id=None,
            device_id=device.id,
            device_name=_NAME,
            device_ip="192.0.2.99",
            question=f"{_NAME} once",
            status="completed",
            created_at=_OLD,
        )
        db.add(run)
        db.commit()
        assert cleanup_old_agent_runs(90) == 1
        assert cleanup_old_agent_runs(90) == 0
    finally:
        _teardown(db, room, rack, device)
