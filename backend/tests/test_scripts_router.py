"""scripts 路由测试：批量命令执行 / 电源操作 / 限流装饰器。

外部命令通道（SSH/WinRM/电源）全部用 mock 隔离，绝不真连设备或执行真实命令。
端点测试走真实 app + 真实测试库（角色/用户/设备自建自清），与 test_metrics_api 一致。
限流既验证装饰器已登记，也通过真实 app 打到 429 验证统一错误格式。
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.middleware.rate_limiter import _is_exempt
from app.middleware.rate_limiter import limiter as app_limiter
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.routers.scripts import (
    PowerExecuteRequest,
    ScriptExecuteRequest,
    execute_power,
    execute_script,
    list_devices_for_scripts,
)
from app.services.auth import create_access_token
from app.services.crypto import encrypt
from app.services.device_credentials import set_credentials
from app.services.script_exec import _resolve_creds, run_script_on_device

client = TestClient(app)


# ── 脚手架 ──


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_role_user(db: Session, permissions: list[str], scope: str = "all"):
    stamp = time.time_ns()
    role = Role(
        name=f"scripts_role_{stamp}",
        permissions=json.dumps(permissions),
        device_scope=scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"scriptsuser_{stamp}",
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
        device_scope=scope,
    )
    return role, user, token


def _cleanup(db, user_ids, role_ids, device_ids, rack_ids, room_ids):
    db.query(Device).filter(Device.id.in_(device_ids)).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(Role).filter(Role.id.in_(role_ids)).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.id.in_(rack_ids)).delete(synchronize_session=False)
    db.query(Room).filter(Room.id.in_(room_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def env():
    """automation:manage 用户 + 一个机柜下的两台 Linux 设备；用例结束自动清理。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    role, user, token = _create_role_user(
        db, ["automation:manage", "device:view", "device:remote"]
    )
    room = Room(name="Scripts Room", location="L1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="Rack-S", type="cabinet")
    db.add(rack)
    db.flush()
    dev1 = Device(
        rack_id=rack.id,
        name="srv-a",
        type="server",
        ip_address="10.2.0.1",
        os_system="Rocky Linux 9.4",
        status="online",
    )
    dev2 = Device(
        rack_id=rack.id,
        name="srv-b",
        type="server",
        ip_address="10.2.0.2",
        os_system="Ubuntu 24.04",
        status="online",
    )
    db.add_all([dev1, dev2])
    db.commit()
    set_credentials(dev1, "root", "pw1", None)
    set_credentials(dev2, "root", "pw2", None)
    db.commit()
    try:
        yield SimpleNamespace(
            db=db,
            token=token,
            role=role,
            user=user,
            room=room,
            rack=rack,
            dev1=dev1,
            dev2=dev2,
        )
    finally:
        _cleanup(db, [user.id], [role.id], [dev1.id, dev2.id], [rack.id], [room.id])
        db.close()


# ── Schema 校验 ──


def test_script_request_dedups_device_ids():
    body = ScriptExecuteRequest(device_ids=[3, 1, 1, 2, 3], command="ls")
    assert body.device_ids == [3, 1, 2]  # 去重且保留首次出现顺序


def test_script_request_rejects_empty_devices_and_command():
    with pytest.raises(ValidationError):
        ScriptExecuteRequest(device_ids=[], command="ls")
    with pytest.raises(ValidationError):
        ScriptExecuteRequest(device_ids=[1], command="")


def test_script_request_timeout_bounds():
    assert ScriptExecuteRequest(device_ids=[1], command="x").timeout == 30
    with pytest.raises(ValidationError):
        ScriptExecuteRequest(device_ids=[1], command="x", timeout=0)
    with pytest.raises(ValidationError):
        ScriptExecuteRequest(device_ids=[1], command="x", timeout=301)


def test_power_request_dedups_and_requires_action():
    body = PowerExecuteRequest(device_ids=[1, 1, 2], action="reboot")
    assert body.device_ids == [1, 2]
    with pytest.raises(ValidationError):
        PowerExecuteRequest(device_ids=[1], action="")


# ── _resolve_creds ──


class _StubDevice:
    def __init__(
        self,
        id=1,
        name="d",
        ip="10.0.0.1",
        os_system="linux",
        username="root",
        password="pw",
    ):
        self.id = id
        self.name = name
        self.ip_address = ip
        self.os_system = os_system
        self.remote_username = username
        self.remote_password_enc = encrypt(password) if password else None
        self.remote_ssh_key_enc = None

    @property
    def is_windows(self):
        return "windows" in (self.os_system or "").lower()


def test_resolve_creds_defaults_to_root_when_device_has_no_username():
    username, _, _ = _resolve_creds(_StubDevice(username=None), None, None, None)
    assert username == "root"


def test_resolve_creds_prefers_request_username():
    username, password, _ = _resolve_creds(
        _StubDevice(username="dev"), "req", "pw", None
    )
    assert username == "req"
    assert password == "pw"


def test_resolve_creds_uses_device_username():
    username, _, _ = _resolve_creds(_StubDevice(username="dev"), None, None, None)
    assert username == "dev"


# ── run_script_on_device（线程内单机执行；内核已下沉 services/script_exec.py）──


def test_run_on_device_linux_success():
    with patch(
        "app.services.script_exec._exec_ssh_command", return_value=(0, "out", "")
    ) as m:
        result = run_script_on_device(
            _StubDevice(id=7, name="srv", ip="10.0.0.7"), "uptime", 30
        )
    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "out"
    assert result.device_id == 7
    assert result.device_name == "srv"
    assert result.ip_address == "10.0.0.7"
    m.assert_called_once()


def test_run_on_device_linux_nonzero_exit():
    with patch(
        "app.services.script_exec._exec_ssh_command", return_value=(2, "", "err")
    ):
        result = run_script_on_device(_StubDevice(), "false", 30)
    assert result.success is False
    assert result.exit_code == 2
    assert result.stderr == "err"


def test_run_on_device_linux_exception_captured():
    with patch(
        "app.services.script_exec._exec_ssh_command", side_effect=RuntimeError("boom")
    ):
        result = run_script_on_device(_StubDevice(), "x", 30)
    assert result.success is False
    assert result.exit_code is None
    assert "boom" in result.error


def test_run_on_device_windows_uses_winrm():
    win = _StubDevice(os_system="Microsoft Windows Server 2019")
    with (
        patch("app.services.winrm.run_on_device", return_value=(0, "winout", "")) as w,
        patch("app.services.script_exec._exec_ssh_command") as s,
    ):
        result = run_script_on_device(win, "Get-Date", 30)
    assert result.success is True
    assert result.stdout == "winout"
    w.assert_called_once()
    s.assert_not_called()  # Windows 不走 SSH


# ── 端点：设备列表 ──


def test_list_devices_for_scripts(env):
    resp = client.get("/api/scripts/devices", headers=_auth(env.token))
    assert resp.status_code == 200
    rows = {d["id"]: d for d in resp.json()}
    assert env.dev1.id in rows and env.dev2.id in rows
    entry = rows[env.dev1.id]
    assert entry["name"] == "srv-a"
    assert entry["ip_address"] == "10.2.0.1"
    assert entry["type"] == "server"
    assert entry["credential_id"] is None


def test_list_devices_requires_permission():
    db = SessionLocal()
    role, user, token = _create_role_user(db, [])  # 无 automation:manage
    db.commit()
    try:
        resp = client.get("/api/scripts/devices", headers=_auth(token))
        assert resp.status_code == 403
    finally:
        _cleanup(db, [user.id], [role.id], [], [], [])
        db.close()


# ── 端点：命令执行 ──


def test_execute_script_success(env):
    payload = {"device_ids": [env.dev1.id], "command": "uptime", "timeout": 10}
    with patch(
        "app.services.script_exec._exec_ssh_command", return_value=(0, "hi", "")
    ):
        resp = client.post(
            "/api/scripts/execute", json=payload, headers=_auth(env.token)
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["succeeded"] == 1
    assert data["failed"] == 0
    assert data["results"][0]["success"] is True
    assert data["results"][0]["stdout"] == "hi"


def test_execute_script_results_sorted_by_device_id(env):
    payload = {"device_ids": [env.dev2.id, env.dev1.id], "command": "echo ok"}
    with patch(
        "app.services.script_exec._exec_ssh_command", return_value=(0, "ok", "")
    ):
        resp = client.post(
            "/api/scripts/execute", json=payload, headers=_auth(env.token)
        )
    assert resp.status_code == 200
    ids = [r["device_id"] for r in resp.json()["results"]]
    assert ids == sorted(ids)
    assert set(ids) == {env.dev1.id, env.dev2.id}


def test_execute_script_device_not_found(env):
    payload = {"device_ids": [999999999], "command": "uptime"}
    resp = client.post("/api/scripts/execute", json=payload, headers=_auth(env.token))
    assert resp.status_code == 404


def test_execute_script_blank_command_rejected(env):
    payload = {"device_ids": [env.dev1.id], "command": "   "}
    resp = client.post("/api/scripts/execute", json=payload, headers=_auth(env.token))
    assert resp.status_code == 400
    assert "命令" in resp.json()["detail"]


def test_execute_script_requires_permission():
    db = SessionLocal()
    role, user, token = _create_role_user(db, ["device:view"])  # 缺 automation:manage
    db.commit()
    try:
        resp = client.post(
            "/api/scripts/execute",
            json={"device_ids": [1], "command": "x"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _cleanup(db, [user.id], [role.id], [], [], [])
        db.close()


# ── 端点：电源操作 ──


def test_execute_power_invalid_action(env):
    resp = client.post(
        "/api/scripts/power",
        json={"device_ids": [env.dev1.id], "action": "explode"},
        headers=_auth(env.token),
    )
    assert resp.status_code == 400


def test_execute_power_shutdown_success(env):
    with patch(
        "app.services.script_exec.shutdown_device",
        return_value={"success": True, "message": "down"},
    ):
        resp = client.post(
            "/api/scripts/power",
            json={"device_ids": [env.dev1.id], "action": "shutdown"},
            headers=_auth(env.token),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "shutdown"
    assert data["succeeded"] == 1
    assert data["results"][0]["success"] is True
    assert data["results"][0]["message"] == "down"


def test_execute_power_reboot_success(env):
    with patch(
        "app.services.script_exec.reboot_device",
        return_value={"success": True, "message": "rebooting"},
    ):
        resp = client.post(
            "/api/scripts/power",
            json={"device_ids": [env.dev2.id], "action": "reboot"},
            headers=_auth(env.token),
        )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["message"] == "rebooting"


def test_execute_power_device_not_found(env):
    resp = client.post(
        "/api/scripts/power",
        json={"device_ids": [999999999], "action": "shutdown"},
        headers=_auth(env.token),
    )
    assert resp.status_code == 404


# ── 限流装饰器 ──


def test_rate_limit_decorators_registered():
    """昂贵端点各自登记了独立限额，且对中间件豁免（由装饰器计数）。"""
    limits = app_limiter._route_limits
    assert "app.routers.scripts.execute_script" in limits
    assert "app.routers.scripts.execute_power" in limits
    assert (
        str(limits["app.routers.scripts.execute_script"][0].limit) == "30 per 1 minute"
    )
    assert (
        str(limits["app.routers.scripts.execute_power"][0].limit) == "10 per 1 minute"
    )
    # 被装饰的端点对中间件豁免；未装饰的列表端点不豁免
    assert _is_exempt(app_limiter, execute_script) is True
    assert _is_exempt(app_limiter, execute_power) is True
    assert _is_exempt(app_limiter, list_devices_for_scripts) is False


def test_execute_script_rate_limit_returns_429(env):
    """真实打到 30/minute 上限：第 31 次返回项目统一格式的 429。"""
    payload = {"device_ids": [env.dev1.id], "command": "uptime"}
    codes = []
    try:
        with patch(
            "app.services.script_exec._exec_ssh_command", return_value=(0, "", "")
        ):
            for _ in range(31):
                codes.append(
                    client.post(
                        "/api/scripts/execute", json=payload, headers=_auth(env.token)
                    ).status_code
                )
        assert codes[:30] == [200] * 30, codes
        assert codes[30] == 429
        # 再请求一次确认 429 响应体格式
        with patch(
            "app.services.script_exec._exec_ssh_command", return_value=(0, "", "")
        ):
            resp = client.post(
                "/api/scripts/execute", json=payload, headers=_auth(env.token)
            )
        assert resp.status_code == 429
        data = resp.json()
        assert data["success"] is False
        assert data["code"] == "RATE_LIMIT_EXCEEDED"
        assert "频繁" in data["message"]
        assert resp.headers.get("retry-after")
    finally:
        app_limiter.reset()  # 清空内存计数，避免影响其它用例
