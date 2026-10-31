"""实时 Agent 分析诊断与执行记录的统一入口(2026-10-30)。

diagnose 端点发起时同步落一条 job_type='agent' 的 automation job:
- 执行记录/概览统计与其它三类任务同一入口可见;
- target.result_json 预写 legacy_run_id,任务详情抽屉运行中即可跳时间线;
- run 终态由 _execute_run 回写任务状态(finish_agent_job);
- 服务重启由 recover_interrupted_jobs 兜底(既有行为,不在此重复钉)。
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.agent_run import AgentRun
from app.models.automation import AutomationJob, AutomationJobStep, AutomationJobTarget
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services.agent import AgentConfig
from app.services.auth import create_access_token

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).timestamp()}"


def _mk_role_user(db: Session, permissions: list[str]) -> tuple[Role, User, str]:
    role = Role(
        name=_unique("agj_role"),
        permissions=json.dumps(permissions),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=_unique("agjuser"),
        password="irrelevant",
        role="viewer",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=permissions,
        device_scope="all",
    )
    return role, user, token


def _fake_cfg() -> AgentConfig:
    return AgentConfig(
        enabled=True,
        base_url="http://llm.local/v1",
        api_key="sk-test",
        model="test-model",
        max_steps=4,
    )


@pytest.fixture()
def env():
    """自建设备与用户;测试结束精确清理本 fixture 创建的行(含任务/运行记录)。"""
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    role, user, token = _mk_role_user(db, ["device:remote", "automation:manage"])

    room = Room(name=_unique("agj_room"), location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=_unique("agj_rack"), type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=_unique("agj_dev"),
        type="server",
        ip_address="10.99.9.1",
        status="online",
        ssh_port=22,
        rdp_port=3389,
    )
    db.add(device)
    db.commit()

    try:
        yield db, user, token, device
    finally:
        job_ids = [
            j.id
            for j in db.query(AutomationJob)
            .filter(AutomationJob.name.like(f"{device.name}%"))
            .all()
        ]
        if job_ids:
            target_ids = [
                t.id
                for t in db.query(AutomationJobTarget)
                .filter(AutomationJobTarget.job_id.in_(job_ids))
                .all()
            ]
            if target_ids:
                db.query(AutomationJobStep).filter(
                    AutomationJobStep.target_id.in_(target_ids)
                ).delete()
            db.query(AutomationJobTarget).filter(
                AutomationJobTarget.job_id.in_(job_ids)
            ).delete()
            db.query(AutomationJob).filter(AutomationJob.id.in_(job_ids)).delete()
        db.query(AgentRun).filter(AgentRun.device_name == device.name).delete()
        db.query(Device).filter(Device.id == device.id).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


def _diagnose(token: str, device_id: int, question: str, extra: dict | None = None):
    body: dict = {"device_id": device_id, "question": question}
    body.update(extra or {})
    return client.post(
        "/api/agent/diagnose", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def test_diagnose_creates_job_record(env):
    """发起即落任务记录(模式固定 strict)、target 预写 legacy_run_id。"""
    db, user, token, device = env
    with (
        patch("app.routers.agent.get_agent_config", return_value=_fake_cfg()),
        patch("app.services.agent._execute_run") as mock_exec,
    ):
        resp = _diagnose(token, device.id, "磁盘占用为什么这么高")
        assert resp.status_code == 200
        body = resp.json()
        assert mock_exec.called  # 后台线程已拉起(被 mock,不真跑诊断)
        assert body["mode"] == "strict"  # 模式已删,恒为 strict(写动作全审批)

    db.rollback()  # HTTP 提交对快照不可见(REPEATABLE READ),刷新后再查
    job = db.query(AutomationJob).filter(AutomationJob.id == body["job_id"]).first()
    assert job is not None
    assert job.job_type == "agent"
    assert job.trigger_type == "manual"
    assert job.status == "running"
    assert job.created_by == user.id
    assert job.config_json["question"] == "磁盘占用为什么这么高"
    assert job.config_json["mode"] == "strict"
    assert job.risk_level == "high"  # 模式固定 strict,风险恒 high

    run = db.query(AgentRun).filter(AgentRun.id == body["run_id"]).first()
    assert run is not None and run.mode == "strict"

    target = job.targets[0]
    assert target.device_id == device.id
    assert target.status == "running"
    # 预写链接:任务详情抽屉运行中即可打开诊断时间线
    assert target.result_json == {"legacy_run_id": body["run_id"]}


def test_diagnose_strict_without_manage_permission(env):
    """发起权限不再耦合 automation:manage(模式删除后写安全全在审批闸门):
    仅持 device:remote 的用户可发起;旧 body.mode 字段被忽略。"""
    db, user, _token, device = env
    from app.services.auth import create_access_token as _cat

    ro_token = _cat(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["device:remote"],
        device_scope="all",
    )
    with (
        patch("app.routers.agent.get_agent_config", return_value=_fake_cfg()),
        patch("app.services.agent._execute_run"),
    ):
        resp = _diagnose(
            ro_token, device.id, "排查服务无法启动", extra={"mode": "readonly"}
        )
        assert resp.status_code == 200
    db.rollback()
    run = (
        db.query(AgentRun)
        .filter(AgentRun.device_name == device.name)
        .order_by(AgentRun.id.desc())
        .first()
    )
    assert run.mode == "strict"  # 请求体里的旧 mode 字段被忽略


def test_execute_run_syncs_job_terminal_state(env):
    """run 终态回写任务记录:completed 路径 job/target 同步收尾并带报告。"""
    db, user, token, device = env
    from app.services.automation import create_job_record

    job = create_job_record(
        db,
        name="磁盘占用为什么这么高",
        job_type="agent",
        device_ids=[device.id],
        config={"question": "磁盘占用为什么这么高", "user_id": user.id},
        user_id=user.id,
        user_name=user.username,
    )
    target = job.targets[0]
    now = datetime.now(timezone.utc)
    job.status = "running"
    job.started_at = now
    target.status = "running"
    target.started_at = now
    db.commit()

    run = AgentRun(
        user_id=user.id,
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question="磁盘占用为什么这么高",
        status="running",
        created_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    from app.services.agent import _execute_run

    with patch("app.services.agent._loop", return_value="诊断报告正文"):
        _execute_run(
            run.id,
            device,
            "磁盘占用为什么这么高",
            _fake_cfg(),
            None,
            False,
            "readonly",
            job.id,
            target.id,
        )

    db.expire_all()
    db.rollback()  # _execute_run 用独立会话写入,刷新本会话快照
    run_row = db.query(AgentRun).filter(AgentRun.id == run.id).first()
    assert run_row.status == "completed"
    assert run_row.report == "诊断报告正文"

    job_row = db.query(AutomationJob).filter(AutomationJob.id == job.id).first()
    assert job_row.status == "completed"
    assert job_row.summary_json == {
        "total": 1,
        "succeeded": 1,
        "warnings": 0,
        "failed": 0,
    }
    target_row = (
        db.query(AutomationJobTarget)
        .filter(AutomationJobTarget.id == target.id)
        .first()
    )
    assert target_row.status == "completed"
    assert target_row.result_json["legacy_run_id"] == run.id
    assert target_row.result_json["report"] == "诊断报告正文"


def test_resolve_diagnose_target_negative_id_carries_device_name(env):
    """虚机 diagnose 不再 500:_pve_runtime_device 拼 AgentTarget 要
    target.device_name,而本端点传的是 SimpleNamespace——漏属性即
    AttributeError(2026-10-30 用户实测启动失败)。钉住:快照名优先、
    VM {vmid} 兑底,其余三元组字段不变形。"""
    import app.services.automation as automation_mod
    import app.services.pve_guest_status as status_mod
    from app.routers.agent import _resolve_diagnose_target

    db, user, token, device = env
    captured = {}
    sentinel = object()

    def fake_runtime(target):
        captured["ns"] = target
        return sentinel  # 非 None 即视为可诊断

    synthetic = -(2 * 1_000_000 + 101)
    with (
        patch.object(
            status_mod,
            "get_guests_by_connection",
            return_value={101: {"name": "win2022-dcn"}},
        ),
        patch.object(automation_mod, "_pve_runtime_device", fake_runtime),
        patch("app.routers.agent.user_can_access_pve_vmid", return_value=True),
    ):
        resolved = _resolve_diagnose_target(synthetic, db, user)
    assert resolved is sentinel
    ns = captured["ns"]
    assert ns.device_name == "win2022-dcn"
    assert (ns.pve_connection_id, ns.pve_guest_type, ns.pve_vmid) == (2, "qemu", 101)

    # 快照缺失(后端刚重启未刷新):回退 VM {vmid},同样不炸
    with (
        patch.object(status_mod, "get_guests_by_connection", return_value={}),
        patch.object(automation_mod, "_pve_runtime_device", fake_runtime),
        patch("app.routers.agent.user_can_access_pve_vmid", return_value=True),
    ):
        _resolve_diagnose_target(synthetic, db, user)
    assert captured["ns"].device_name == "VM 101"
