"""PVE 虚拟机 RDP 控制台回归测试。

曾经 ``pve_rdp_ws`` 以 ``enable_drive=False`` 调用 ``_handle_rdp``，guacd 因此
不下发 ``filesystem`` 指令，前端 ``isFilesystemReady`` 永远为假，文件管理器的
「上传到当前目录」按钮被禁用——点击毫无反应，且没有任何报错。这里用录制的
kwargs 把该参数钉死。
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.role import Role
from app.models.user import User
from app.routers import pve_console
from app.services.crypto import encrypt
from app.services.ws_ticket import issue_ticket

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()


@pytest.fixture()
def guest_rdp_env():
    """建好「用户(pve:manage) + PVE 连接 + 已配接入的 Windows 虚机」。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    role = Role(
        name=f"rdp_role_{STAMP}",
        permissions='["pve:manage"]',
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"rdpuser_{STAMP}",
        password="irrelevant",
        role="viewer",
        is_active=1,
        role_id=role.id,
        session_version=3,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"rdp-pve-{STAMP}",
        host="10.9.0.66",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=102,
        ip_address="10.9.0.102",
        os_system="windows",
        username="administrator",
        password_enc=encrypt("S3cret!pass"),
        enabled=1,
    )
    db.add(binding)
    db.commit()

    yield {
        "user_id": user.id,
        "conn_id": conn.id,
        "binding_id": binding.id,
        "vmid": binding.vmid,
    }

    db.query(PveGuestBinding).filter(PveGuestBinding.connection_id == conn.id).delete(
        synchronize_session=False
    )
    db.query(PveConnection).filter(PveConnection.id == conn.id).delete(
        synchronize_session=False
    )
    db.query(User).filter(User.id == user.id).delete(synchronize_session=False)
    db.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
    db.commit()
    db.close()


def _ticket(env: dict) -> str:
    return issue_ticket(
        {
            "kind": "pve-remote",
            "mode": "rdp",
            "binding_id": env["binding_id"],
            "conn_id": env["conn_id"],
            "gtype": "qemu",
            "vmid": env["vmid"],
            "target_ip": "10.9.0.102",
            "width": 1280,
            "height": 720,
            "user_id": env["user_id"],
            "session_version": 3,
        }
    )


def test_pve_rdp_enables_guacamole_drive(guest_rdp_env):
    """虚拟盘必须开启，否则前端文件管理器拿不到 filesystem 对象。"""
    recorded: dict = {}

    async def _recorder(_websocket, _device_id, _username, _password, **kwargs):
        recorded.update(kwargs)

    with patch.object(pve_console, "_handle_rdp", _recorder):
        with client.websocket_connect(
            f"/ws/pve-rdp?ticket={_ticket(guest_rdp_env)}&session_id=sess-abc"
        ):
            pass

    assert recorded.get("enable_drive") is True, (
        "enable_drive 必须为 True：关掉后 guacd 不下发 filesystem 指令，"
        "RDP 文件上传按钮会被前端禁用（点击无反应）"
    )
    assert recorded.get("session_id") == "sess-abc"
    assert recorded.get("target_host") == "10.9.0.102"
    assert recorded.get("target_port") == 3389


def test_pve_rdp_rejects_disabled_binding(guest_rdp_env):
    """接入被停用时直接关闭，不应进入 _handle_rdp。"""
    db = SessionLocal()
    try:
        db.query(PveGuestBinding).filter(
            PveGuestBinding.id == guest_rdp_env["binding_id"]
        ).update({"enabled": 0})
        db.commit()
    finally:
        db.close()

    called = False

    async def _recorder(*_a, **_k):
        nonlocal called
        called = True

    # 该分支是「先 accept 再 close(4000)」，所以连接能建立，要从关闭帧判定。
    # Starlette 测试会话在这里返回原始 close 消息而不是抛 WebSocketDisconnect。
    with patch.object(pve_console, "_handle_rdp", _recorder):
        with client.websocket_connect(
            f"/ws/pve-rdp?ticket={_ticket(guest_rdp_env)}"
        ) as ws:
            message = ws.receive()
            assert message["type"] == "websocket.close"
            assert message["code"] == 4000

    assert called is False
