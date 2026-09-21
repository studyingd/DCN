from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.automation import (
    AutomationJob,
    AutomationJobStep,
    AutomationJobTarget,
    AutomationSchedule,
)
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services.auth import create_access_token
from app.services.automation import _execute_inspection

client = TestClient(app)


def _fixture():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    role = Role(
        name=f"automation_role_{suffix}",
        permissions='["device:remote","automation:manage"]',
        device_scope="all",
    )
    room = Room(name=f"automation_room_{suffix}")
    db.add_all([role, room])
    db.flush()
    rack = Rack(room_id=room.id, name=f"automation_rack_{suffix}", type="cabinet")
    user = User(
        username=f"automation_user_{suffix}",
        password="irrelevant",
        role="operator",
        is_active=1,
        role_id=role.id,
    )
    db.add_all([rack, user])
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"automation_device_{suffix}",
        type="server",
        ip_address="192.0.2.80",
        status="online",
        os_system="Linux",
        # create_job 要求设备绑定运维凭据(与 PVE 虚拟机目标同口径),
        # 否则提交时 400。
        remote_username="root",
        remote_password_enc="enc:automation",
    )
    db.add(device)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["device:remote", "automation:manage"],
        device_scope="all",
    )
    return db, role, room, rack, user, device, {"Authorization": f"Bearer {token}"}


def _cleanup(db, role, room, rack, user, device):
    db.query(AutomationJobTarget).filter(
        AutomationJobTarget.device_id == device.id
    ).delete()
    db.query(AutomationJob).filter(AutomationJob.created_by == user.id).delete()
    db.query(AutomationSchedule).filter(
        AutomationSchedule.created_by == user.id
    ).delete()
    db.delete(device)
    db.delete(user)
    db.delete(rack)
    db.delete(room)
    db.delete(role)
    db.commit()
    db.close()


def test_unified_job_creation_and_detail(monkeypatch):
    db, role, room, rack, user, device, headers = _fixture()
    monkeypatch.setattr(
        "app.routers.automation.start_automation_job", lambda _job_id: None
    )
    response = client.post(
        "/api/automation/jobs",
        headers=headers,
        json={
            "name": "服务器健康诊断",
            "job_type": "agent",
            "device_ids": [device.id],
            "config": {"question": "检查服务器健康状态"},
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["job_type"] == "agent"
    assert data["risk_level"] == "read_only"
    assert data["targets"][0]["device_id"] == device.id

    detail = client.get(f"/api/automation/jobs/{data['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["name"] == "服务器健康诊断"
    _cleanup(db, role, room, rack, user, device)


def test_automation_devices_metrics_failed_only_for_windows():
    """/devices 的 metrics_failed:仅 Windows 且最近采集明确失败时为 True。

    Linux 采集失败、Windows 采集成功、尚未采集过均不标记——选择性器只对
    「WinRM 确认不通」的 Windows 目标置灰,其余给修复场景留路。
    """
    from unittest.mock import patch

    db, role, room, rack, user, device, headers = _fixture()
    # fixture 设备是 Linux;建一台 Windows 设备对照
    win = Device(
        rack_id=rack.id,
        name=f"automation_win_{device.id}",
        type="server",
        ip_address="192.0.2.81",
        status="online",
        os_system="Microsoft Windows Server 2019",
        remote_username="admin",
        remote_password_enc="enc:win",
    )
    db.add(win)
    db.commit()

    cases = {
        device.id: {"available": True},  # Linux 采集成功
        win.id: {"available": False},  # Windows 采集失败
    }
    try:
        with patch("app.routers.automation.get_latest_metrics", return_value=cases):
            resp = client.get("/api/automation/devices", headers=headers)
        assert resp.status_code == 200
        by_id = {d["id"]: d for d in resp.json()}
        assert by_id[device.id]["metrics_failed"] is False  # Linux 不看采集
        assert by_id[win.id]["metrics_failed"] is True  # Windows 失败 → 标记

        # Windows 采集成功/未采集 → 不标记
        for win_case in ({"available": True}, {}):
            with patch(
                "app.routers.automation.get_latest_metrics",
                return_value={**cases, win.id: win_case},
            ):
                resp = client.get("/api/automation/devices", headers=headers)
            by_id = {d["id"]: d for d in resp.json()}
            assert by_id[win.id]["metrics_failed"] is False
    finally:
        db.query(Device).filter(Device.id == win.id).delete()
        db.commit()
        _cleanup(db, role, room, rack, user, device)


def test_unified_job_rejects_device_without_credential(monkeypatch):
    """普通设备未绑凭据 → 提交时 400,与 PVE 虚拟机目标同一口径。

    电源任务同样要拦:普通设备的关机/重启也是 SSH/WinRM 连进机器执行,
    没有带外通道,与 PVE 虚拟机(走 PVE API)不同。
    """
    db, role, room, rack, user, device, headers = _fixture()
    device.remote_username = None
    device.remote_password_enc = None
    db.commit()
    monkeypatch.setattr(
        "app.routers.automation.start_automation_job", lambda _job_id: None
    )
    for job_type in ("agent", "inspection", "script", "power"):
        payload = {
            "name": "无凭据目标",
            "job_type": job_type,
            "device_ids": [device.id],
        }
        if job_type == "agent":
            payload["config"] = {"question": "检查服务器健康状态"}
        if job_type == "power":
            payload["config"] = {"action": "shutdown"}
        response = client.post("/api/automation/jobs", headers=headers, json=payload)
        assert response.status_code == 400, job_type
        assert "尚未配置有效的运维接入" in response.json()["detail"]
    _cleanup(db, role, room, rack, user, device)


def test_unified_recurring_schedule_creation():
    db, role, room, rack, user, device, headers = _fixture()
    response = client.post(
        "/api/automation/schedules",
        headers=headers,
        json={
            "name": "每日快速巡检",
            "job_type": "inspection",
            "device_ids": [device.id],
            "config": {"mode": "quick", "timeout": 30},
            "schedule_type": "recurring",
            "cron_expression": "0 2 * * *",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["job_type"] == "inspection"
    assert data["next_run_at"] is not None

    listing = client.get("/api/automation/schedules", headers=headers)
    assert listing.status_code == 200
    assert any(item["id"] == data["id"] for item in listing.json())
    _cleanup(db, role, room, rack, user, device)


def _job_with_target_statuses(db, user, device, statuses):
    """直接落库一个任务，targets 状态逐个按入参指定（不触发真实执行）。"""
    job = AutomationJob(
        name="健康巡检进度统计",
        job_type="inspection",
        trigger_type="manual",
        status="completed",
        risk_level="read_only",
        config_json={"mode": "core"},
        created_by=user.id,
        created_by_name=user.username,
    )
    db.add(job)
    db.flush()
    for status in statuses:
        db.add(
            AutomationJobTarget(
                job_id=job.id,
                device_id=device.id,
                device_name=device.name,
                device_ip=device.ip_address,
                target_type="device",
                status=status,
            )
        )
    db.commit()
    return job


def _fetch_job_list_item(headers, job_id):
    """从任务列表接口里捞出指定 job 的四个计数器。"""
    response = client.get(
        "/api/automation/jobs", headers=headers, params={"page_size": 100}
    )
    assert response.status_code == 200
    matched = [item for item in response.json()["items"] if item["id"] == job_id]
    assert matched, f"任务列表没有返回 job {job_id}"
    return matched[0]


def _drop_steps(db, target_id):
    db.query(AutomationJobStep).filter(
        AutomationJobStep.target_id == target_id
    ).delete()
    db.commit()


def test_job_list_progress_counts_warning_targets():
    """巡检发现异常时 target 落在 warning，列表计数器必须单独统计它。

    历史 bug：聚合只有 completed / failed / running 三桶，warning 被整体漏掉，
    前端按 (succeeded + failed) / target_total 算进度，于是「任务已完成但进度 0%」。
    """
    db, role, room, rack, user, device, headers = _fixture()
    job = _job_with_target_statuses(
        db, user, device, ["completed", "warning", "failed", "running"]
    )
    item = _fetch_job_list_item(headers, job.id)
    assert item["target_total"] == 4
    assert item["succeeded"] == 1
    assert item["warning"] == 1
    assert item["failed"] == 1
    assert item["running"] == 1
    # warning 不许折进 succeeded，否则会掩盖「存在异常」这个语义。
    assert item["succeeded"] + item["failed"] + item["warning"] == 3
    _cleanup(db, role, room, rack, user, device)


def test_job_list_progress_all_warning_targets_reach_100_percent():
    """线上暴露的场景：全部 target 都是 warning（巡检跑完但每台都有异常）。

    此时进度必须是 100%，而不是 0%。
    """
    db, role, room, rack, user, device, headers = _fixture()
    job = _job_with_target_statuses(db, user, device, ["warning"] * 3)
    item = _fetch_job_list_item(headers, job.id)
    assert item["target_total"] == 3
    assert item["succeeded"] == 0
    assert item["failed"] == 0
    assert item["running"] == 0
    assert item["warning"] == 3
    assert item["succeeded"] + item["failed"] + item["warning"] == item["target_total"]
    _cleanup(db, role, room, rack, user, device)


def test_inspection_executor_defaults_to_core_and_localizes_step_names(monkeypatch):
    """自动化巡检：未配置 mode 时兜底跑 core，步骤名走 ITEM_LABELS 中文名。"""
    db, role, room, rack, user, device, _headers = _fixture()
    captured = {}

    def fake_run_inspection(**kwargs):
        captured.update(kwargs)
        return {
            "record_id": 1,
            "target_type": "linux",
            "status": "partial",
            "total_items": 2,
            "normal_count": 1,
            "warning_count": 1,
            "critical_count": 0,
            "error_count": 0,
            "items": [
                {
                    "item_type": "failed_services",
                    "status": "normal",
                    "command_used": "systemctl --failed",
                    "raw_output": "0 loaded units listed.",
                    "duration_ms": 12,
                },
                {
                    # 标签表里没有的键必须原样回退，不能变成 None。
                    "item_type": "no_such_item",
                    "status": "warning",
                    "command_used": "uptime",
                    "raw_output": "load average: 9.9",
                    "duration_ms": 8,
                },
            ],
        }

    monkeypatch.setattr("app.services.inspection.run_inspection", fake_run_inspection)

    job = _job_with_target_statuses(db, user, device, ["running"])
    target = db.query(AutomationJobTarget).filter_by(job_id=job.id).first()
    _execute_inspection(target, device, {})

    # _append_step / _finish_target 各自开独立会话提交；结束当前事务才能读到新快照。
    db.commit()

    steps = (
        db.query(AutomationJobStep)
        .filter_by(target_id=target.id)
        .order_by(AutomationJobStep.id)
        .all()
    )
    assert captured["mode"] == "core"
    assert [step.step_name for step in steps] == ["失败服务", "no_such_item"]
    # 原始输出仍留在 output 上，由前端收进折叠区。
    assert steps[0].output == "0 loaded units listed."
    # partial → warning，正是进度统计修复要覆盖的状态。
    assert target.status == "warning"
    _drop_steps(db, target.id)
    _cleanup(db, role, room, rack, user, device)


def test_inspection_job_config_accepts_core_mode(monkeypatch):
    """前端已固定发 mode=core，任务配置校验必须放行，否则建任务直接 400。"""
    db, role, room, rack, user, device, headers = _fixture()
    monkeypatch.setattr(
        "app.routers.automation.start_automation_job", lambda _job_id: None
    )

    response = client.post(
        "/api/automation/jobs",
        headers=headers,
        json={
            "name": "健康巡检",
            "job_type": "inspection",
            "device_ids": [device.id],
            "config": {"mode": "core", "timeout": 30},
        },
    )
    assert response.status_code == 200
    assert response.json()["config_json"]["mode"] == "core"

    # 老模式仍可用（巡检中心的历史定时任务配置会带着 quick/standard/custom 进来）。
    legacy = client.post(
        "/api/automation/jobs",
        headers=headers,
        json={
            "name": "健康巡检-快速",
            "job_type": "inspection",
            "device_ids": [device.id],
            "config": {"mode": "quick", "timeout": 30},
        },
    )
    assert legacy.status_code == 200

    # 未登记的 mode 仍然拒绝。
    invalid = client.post(
        "/api/automation/jobs",
        headers=headers,
        json={
            "name": "健康巡检-非法",
            "job_type": "inspection",
            "device_ids": [device.id],
            "config": {"mode": "deep", "timeout": 30},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "巡检模式无效"
    _cleanup(db, role, room, rack, user, device)


# ── Webhook 通知开关语义（默认开）──


def _job_with(config):
    from app.models.automation import AutomationJob

    j = AutomationJob(name="x", job_type="inspection", config_json=config)
    return j


def test_notify_flag_default_on_when_key_missing():
    """缺键 = 开：旧前端不发送该键时也要推送，避免「开了却没发」。"""
    from app.services.automation import _notify_flag

    assert _notify_flag(_job_with({})) is True
    assert _notify_flag(_job_with({"mode": "core", "items": None})) is True


def test_notify_flag_explicit_false_disables():
    from app.services.automation import _notify_flag

    assert _notify_flag(_job_with({"webhook_notify": False})) is False


def test_notify_flag_explicit_true_enables():
    from app.services.automation import _notify_flag

    assert _notify_flag(_job_with({"webhook_notify": True})) is True


def test_notify_flag_legacy_report_webhook_respected():
    from app.services.automation import _notify_flag

    assert _notify_flag(_job_with({"report_webhook": True})) is True
    assert _notify_flag(_job_with({"report_webhook": False})) is False
    # 新键优先于旧键
    assert (
        _notify_flag(_job_with({"webhook_notify": False, "report_webhook": True}))
        is False
    )


def test_maybe_notify_webhook_skips_non_inspection_jobs(monkeypatch):
    """Agent / 批量执行 / 电源操作一律不推 webhook——结果在任务详情可见,
    只有健康巡检发(摘要 + PDF)。即使开关显式打开也不发。"""
    from unittest.mock import MagicMock

    from app.services.automation import _maybe_notify_webhook

    spy = MagicMock(name="send_inspection_report")
    monkeypatch.setattr(
        "app.services.inspection_report.send_inspection_report", spy, raising=True
    )

    for job_type in ("agent", "script", "power"):
        job = _job_with({"webhook_notify": True})
        job.job_type = job_type
        _maybe_notify_webhook(None, job)
        assert spy.call_count == 0, job_type


def test_maybe_notify_webhook_dispatches_inspection(monkeypatch):
    """巡检任务开关打开时走 inspection_report。"""
    from unittest.mock import MagicMock

    from app.services.automation import _maybe_notify_webhook

    spy = MagicMock(name="send_inspection_report")
    monkeypatch.setattr(
        "app.services.inspection_report.send_inspection_report", spy, raising=True
    )
    job = _job_with({"webhook_notify": True})
    _maybe_notify_webhook(None, job)
    assert spy.call_count == 1

    spy.reset_mock()
    job_off = _job_with({"webhook_notify": False})
    _maybe_notify_webhook(None, job_off)
    assert spy.call_count == 0


# ── P1-7:PVE 虚拟机定时计划(与 create_job 同口径) ──


def _pve_fixture(extra_permissions: list[str]):
    """带 pve 权限的夹具:connection + guest binding(凭据齐全) + 用户/角色。"""
    from app.models.pve_connection import PveConnection
    from app.models.pve_guest_binding import PveGuestBinding

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    import json as _json

    perms = _json.dumps(["device:remote", "automation:manage", *extra_permissions])
    role = Role(
        name=f"pve_sched_role_{suffix}",
        permissions=perms,
        device_scope="all",
    )
    db.add(role)
    db.flush()  # 先拿 role.id,再建 user(否则 role_id 落 None → role_ref 永远空)
    user = User(
        username=f"pve_sched_user_{suffix}",
        password="irrelevant",
        role="operator",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"pve-sched-{suffix}",
        host="192.0.2.1",
        token_id="root@pam!t",
        token_secret_enc="enc:irrelevant",
        enabled=1,
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=901,
        ip_address="192.0.2.91",
        os_system="linux",
        username="root",
        password_enc="enc:irrelevant",
        enabled=1,
    )
    db.add(binding)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["device:remote", "automation:manage", *extra_permissions],
    )
    headers = {"Authorization": f"Bearer {token}"}
    return db, role, user, conn, binding, headers


def _pve_cleanup(db, role, user, conn, binding):
    from app.models.pve_connection import PveConnection
    from app.models.pve_guest_binding import PveGuestBinding

    db.query(AutomationSchedule).filter(
        AutomationSchedule.created_by == user.id
    ).delete()
    db.query(AutomationJobTarget).filter(
        AutomationJobTarget.pve_connection_id == conn.id
    ).delete()
    db.query(AutomationJob).filter(AutomationJob.created_by == user.id).delete()
    db.delete(binding)
    db.delete(conn)
    db.delete(user)
    db.delete(role)
    db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
    db.query(PveGuestBinding).filter(PveGuestBinding.id == binding.id).delete()
    db.commit()
    db.close()


def test_schedule_pve_guest_inspection_with_binding():
    """虚机定时计划(非 power):绑定齐全 → 放行,列表可见。"""
    from app.services.containers_collector import pve_target_id

    db, role, user, conn, binding, headers = _pve_fixture(["pve:manage"])
    try:
        response = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "虚机周期巡检",
                "job_type": "inspection",
                "device_ids": [pve_target_id(conn.id, binding.vmid)],
                "config": {"mode": "core", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 3 * * *",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["job_type"] == "inspection"
        assert data["target_ids"] == [pve_target_id(conn.id, binding.vmid)]
        listing = client.get("/api/automation/schedules", headers=headers)
        assert listing.status_code == 200
        assert any(item["id"] == data["id"] for item in listing.json())
    finally:
        _pve_cleanup(db, role, user, conn, binding)


def test_schedule_pve_guest_inspection_without_credentials_rejected():
    """虚机无有效运维接入(非 power)→ 400,与 create_job 同口径。"""
    from app.services.containers_collector import pve_target_id

    db, role, user, conn, binding, headers = _pve_fixture(["pve:manage"])
    try:
        binding.username = None  # 凭据不完整
        db.commit()
        response = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "无凭据虚机巡检",
                "job_type": "inspection",
                "device_ids": [pve_target_id(conn.id, binding.vmid)],
                "config": {"mode": "core", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 4 * * *",
            },
        )
        assert response.status_code == 400
        assert "尚未配置有效的运维接入" in response.json()["detail"]
    finally:
        _pve_cleanup(db, role, user, conn, binding)


def test_schedule_pve_guest_power_without_binding_allowed():
    """power 计划不要求运维接入(走 PVE API);pve:manage 即可。"""
    from app.services.containers_collector import pve_target_id

    db, role, user, conn, binding, headers = _pve_fixture(["pve:manage"])
    try:
        binding.enabled = 0  # 拿掉绑定:电源计划照样放行
        db.commit()
        response = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "虚机定时重启",
                "job_type": "power",
                "device_ids": [pve_target_id(conn.id, binding.vmid)],
                "config": {"action": "reboot"},
                "schedule_type": "recurring",
                "cron_expression": "0 5 * * *",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["job_type"] == "power"
    finally:
        binding.enabled = 1
        _pve_cleanup(db, role, user, conn, binding)


def test_scheduler_creates_job_with_pve_targets(monkeypatch):
    """到期调度:负数 target_id 解回 (conn,vmid),job 落 pve_guest 目标。"""
    import asyncio
    from datetime import timedelta

    from app.services.containers_collector import pve_target_id
    from app.services.scheduler import _check_and_run_due_automation_schedules

    db, role, user, conn, binding, _headers = _pve_fixture(["pve:manage"])
    try:
        target_id = pve_target_id(conn.id, binding.vmid)
        schedule = AutomationSchedule(
            name="到期虚机巡检",
            job_type="inspection",
            target_ids=[target_id],
            config_json={"mode": "core", "timeout": 30},
            schedule_type="recurring",
            cron_expression="0 6 * * *",
            status="active",
            next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            created_by=user.id,
            created_by_name=user.username,
        )
        db.add(schedule)
        db.commit()

        monkeypatch.setattr(
            "app.services.scheduler.start_automation_job", lambda _job_id: None
        )
        asyncio.run(_check_and_run_due_automation_schedules(loop=None))

        job = (
            db.query(AutomationJob)
            .filter(AutomationJob.created_by == user.id)
            .order_by(AutomationJob.id.desc())
            .first()
        )
        assert job is not None, "到期计划没有创建统一任务"
        assert job.trigger_type == "scheduled"
        assert len(job.targets) == 1
        t = job.targets[0]
        assert t.target_type == "pve_guest"
        assert t.pve_connection_id == conn.id
        assert t.pve_vmid == binding.vmid
        assert t.device_id is None
        # 调度后计划推进到下个周期,保持 active
        db.refresh(schedule)
        assert schedule.status == "active"
        assert schedule.next_run_at is not None
    finally:
        _pve_cleanup(db, role, user, conn, binding)


# ── P0-2:自动化任务目标并发执行 ──


def _job_with_targets(db, user, devices, job_type: str = "inspection"):
    job = AutomationJob(
        name="并发执行测试",
        job_type=job_type,
        trigger_type="manual",
        status="pending",
        risk_level="read_only",
        config_json={"mode": "core", "timeout": 30},
        created_by=user.id,
        created_by_name=user.username,
    )
    db.add(job)
    db.flush()
    for d in devices:
        db.add(
            AutomationJobTarget(
                job_id=job.id,
                device_id=d.id,
                device_name=d.name,
                device_ip=d.ip_address,
                target_type="device",
                status="pending",
            )
        )
    db.commit()
    return job


def _make_devices(db, rack, n: int, suffix: str):
    devices = []
    for i in range(n):
        d = Device(
            rack_id=rack.id,
            name=f"conc_device_{suffix}_{i}",
            type="server",
            ip_address=f"192.0.2.{(i % 250) + 1}",
            status="online",
            os_system="Linux",
            remote_username="root",
            remote_password_enc="enc:conc",
        )
        db.add(d)
        devices.append(d)
    db.commit()
    return devices


def test_execute_job_runs_targets_concurrently(monkeypatch):
    """多目标任务并发执行:并发数受限、每台落终态、汇总正确。"""
    import threading
    import time as _time

    from app.services import automation as auto

    db, role, room, rack, user, device, _headers = _fixture()
    devices = [device] + _make_devices(db, rack, 7, "a")  # 8 台
    job = _job_with_targets(db, user, devices)
    job_id = job.id
    target_ids = [t.id for t in job.targets]

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_inspection(target, dev, config):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        _time.sleep(0.15)  # 让并发窗口可观测
        with lock:
            active -= 1
        # 模拟一半失败:失败隔离必须保持
        if dev.name.endswith("_3"):
            raise RuntimeError("boom on 3")
        _append_finish(target)

    def _append_finish(target):
        from app.services.automation import _finish_target

        _finish_target(target.id, "completed", {"ok": True})

    monkeypatch.setattr(auto, "_execute_inspection", fake_inspection)
    monkeypatch.setattr(auto, "AUTOMATION_MAX_CONCURRENT_TARGETS", 4)

    auto._execute_job(job_id)
    db.commit()

    job = db.query(AutomationJob).filter_by(id=job_id).first()
    statuses = {t.device_name: t.status for t in job.targets}
    assert sum(v == "completed" for v in statuses.values()) == 7
    assert sum(v == "failed" for v in statuses.values()) == 1
    assert job.status == "partial"
    assert job.summary_json == {"total": 8, "succeeded": 7, "warnings": 0, "failed": 1}
    # 并发确实发生(>1);且被封顶在 4 以内
    assert peak > 1, "targets did not run concurrently"
    assert peak <= 4, f"concurrency exceeded cap: {peak}"

    # 展示顺序:targets 恒按 id 排序,与执行完成顺序无关
    assert [t.id for t in job.targets] == sorted(target_ids)
    _cleanup(db, role, room, rack, user, device)


def test_execute_job_serial_when_concurrency_one(monkeypatch):
    """AUTOMATION_MAX_CONCURRENT_TARGETS=1 → 恢复逐台串行(回归开关)。"""
    import threading

    from app.services import automation as auto

    db, role, room, rack, user, device, _headers = _fixture()
    devices = [device] + _make_devices(db, rack, 3, "b")
    job = _job_with_targets(db, user, devices)
    job_id = job.id

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_inspection(target, dev, config):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        from app.services.automation import _finish_target

        _finish_target(target.id, "completed", {"ok": True})
        with lock:
            active -= 1

    monkeypatch.setattr(auto, "_execute_inspection", fake_inspection)
    monkeypatch.setattr(auto, "AUTOMATION_MAX_CONCURRENT_TARGETS", 1)
    auto._execute_job(job_id)
    db.commit()
    assert peak == 1  # 严格串行
    job = db.query(AutomationJob).filter_by(id=job_id).first()
    assert job.status == "completed"
    assert job.summary_json["succeeded"] == 4
    _cleanup(db, role, room, rack, user, device)


def test_execute_job_single_target_no_thread_pool(monkeypatch):
    """单目标任务直接执行,不开线程池(agent 任务天然单目标,走这条路径)。"""
    from app.services import automation as auto

    db, role, room, rack, user, device, _headers = _fixture()
    job = _job_with_targets(db, user, [device])
    called = {}

    def fake_inspection(target, dev, config):
        called["name"] = dev.name
        from app.services.automation import _finish_target

        _finish_target(target.id, "completed", {"ok": True})

    monkeypatch.setattr(auto, "_execute_inspection", fake_inspection)
    real_pool = auto.ThreadPoolExecutor

    class _NoPool:
        def __init__(self, *a, **kw):
            raise AssertionError("single-target job must not open a thread pool")

    monkeypatch.setattr(auto, "ThreadPoolExecutor", _NoPool)
    auto._execute_job(job.id)
    db.commit()
    assert called["name"] == device.name
    job = db.query(AutomationJob).filter_by(id=job.id).first()
    assert job.status == "completed"
    monkeypatch.setattr(auto, "ThreadPoolExecutor", real_pool)
    _cleanup(db, role, room, rack, user, device)


# ── P1-5:统一计划的编辑/暂停/恢复(旧系统独有能力迁入统一系统) ──


def test_schedule_edit_updates_fields_and_recalc_next_run():
    """编辑计划:改名称/目标/周期,next_run_at 按新表达式重算。"""
    db, role, room, rack, user, device, headers = _fixture()
    try:
        create = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "旧名称",
                "job_type": "script",
                "device_ids": [device.id],
                "config": {"command": "uptime", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 2 * * *",
            },
        )
        assert create.status_code == 200, create.text
        sid = create.json()["id"]
        old_next = create.json()["next_run_at"]

        edit = client.put(
            f"/api/automation/schedules/{sid}",
            headers=headers,
            json={
                "name": "新名称",
                "config": {"command": "df -h", "timeout": 60},
                "cron_expression": "0 5 * * *",
            },
        )
        assert edit.status_code == 200, edit.text
        data = edit.json()
        assert data["name"] == "新名称"
        assert data["config_json"]["command"] == "df -h"
        assert data["config_json"]["timeout"] == 60
        assert data["cron_expression"] == "0 5 * * *"
        assert data["next_run_at"] != old_next  # 周期变了,下次执行时间重算
        assert data["status"] == "active"
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_schedule_edit_rejects_invalid_cron():
    db, role, room, rack, user, device, headers = _fixture()
    try:
        create = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "坏表达式",
                "job_type": "script",
                "device_ids": [device.id],
                "config": {"command": "uptime", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 2 * * *",
            },
        )
        sid = create.json()["id"]
        edit = client.put(
            f"/api/automation/schedules/{sid}",
            headers=headers,
            json={"cron_expression": "not a cron"},
        )
        assert edit.status_code == 400
        assert "cron" in edit.json()["detail"]
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_schedule_pause_and_resume():
    """暂停→恢复:paused 不出现在到期扫描;恢复时过期 next_run 重算到未来。"""
    import asyncio
    from datetime import timedelta

    from app.models.automation import AutomationSchedule as AS
    from app.services.scheduler import _check_and_run_due_automation_schedules

    db, role, room, rack, user, device, headers = _fixture()
    try:
        create = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "暂停测试",
                "job_type": "script",
                "device_ids": [device.id],
                "config": {"command": "uptime", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "* * * * *",  # 每分钟,方便触发
            },
        )
        sid = create.json()["id"]
        db.rollback()  # 刷新事务快照:TestClient 的提交对本会话(REPEATABLE READ)不可见

        # 暂停
        pause = client.patch(
            f"/api/automation/schedules/{sid}/status",
            headers=headers,
            json={"status": "paused"},
        )
        assert pause.status_code == 200
        assert pause.json()["status"] == "paused"
        db.rollback()

        # next_run_at 拨到过去(模拟暂停了很久),到期扫描必须跳过 paused
        row = db.query(AS).filter_by(id=sid).first()
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
        asyncio.run(_check_and_run_due_automation_schedules(loop=None))
        db.expire_all()
        row = db.query(AS).filter_by(id=sid).first()
        assert row.status == "paused"  # 没被扫描执行
        jobs_before = db.query(AutomationJob).filter_by(created_by=user.id).count()

        # 恢复:过期的 next_run_at 重算到未来,不会立刻补跑
        resume = client.patch(
            f"/api/automation/schedules/{sid}/status",
            headers=headers,
            json={"status": "active"},
        )
        assert resume.status_code == 200
        assert resume.json()["status"] == "active"
        db.rollback()  # 刷新事务快照,让 PATCH 的提交对本会话可见
        row = db.query(AS).filter_by(id=sid).first()
        assert row.next_run_at is not None
        assert row.next_run_at > datetime.now(timezone.utc)
        # 恢复后没有立即创建任务(下一轮调度才会)
        assert (
            db.query(AutomationJob).filter_by(created_by=user.id).count() == jobs_before
        )
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_schedule_pause_rejects_invalid_status():
    db, role, room, rack, user, device, headers = _fixture()
    try:
        create = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "非法状态",
                "job_type": "script",
                "device_ids": [device.id],
                "config": {"command": "uptime", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 2 * * *",
            },
        )
        sid = create.json()["id"]
        bad = client.patch(
            f"/api/automation/schedules/{sid}/status",
            headers=headers,
            json={"status": "disabled"},
        )
        assert bad.status_code == 422  # pydantic 拒绝
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_schedule_edit_permission_rejected_for_non_owner():
    """非创建者(且非管理员)编辑/暂停别人计划 → 404(不泄露存在性)。"""
    db, role, room, rack, user, device, headers = _fixture()
    try:
        create = client.post(
            "/api/automation/schedules",
            headers=headers,
            json={
                "name": "别人的计划",
                "job_type": "script",
                "device_ids": [device.id],
                "config": {"command": "uptime", "timeout": 30},
                "schedule_type": "recurring",
                "cron_expression": "0 2 * * *",
            },
        )
        sid = create.json()["id"]

        # 另一个同权限用户
        other = User(
            username=f"other_user_{datetime.now(timezone.utc).timestamp()}".replace(
                ".", ""
            ),
            password="x",
            role="operator",
            is_active=1,
            role_id=role.id,
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        other_token = create_access_token(
            user_id=other.id,
            username=other.username,
            role=other.role,
            permissions=["device:remote", "automation:manage"],
        )
        other_headers = {"Authorization": f"Bearer {other_token}"}

        edit = client.put(
            f"/api/automation/schedules/{sid}",
            headers=other_headers,
            json={"name": "改名"},
        )
        assert edit.status_code == 404
        pause = client.patch(
            f"/api/automation/schedules/{sid}/status",
            headers=other_headers,
            json={"status": "paused"},
        )
        assert pause.status_code == 404
        db.delete(other)
        db.commit()
    finally:
        _cleanup(db, role, room, rack, user, device)
