"""PVE 虚拟机的文件传输接口测试（/api/devices/{负数ID}/files/*）。

虚拟机控制台此前只能敲命令、不能传文件：这四个端点只认 ``devices`` 行，而虚机
的身份是 ``pve_guest_bindings`` 里的 ``connection_id + vmid``（对外表现为与
containers/automation 共用的合成负数 ID）。现在按 ID 正负号分流，权限口径也随之
分成两条：设备走 ``device:remote`` + 设备 ACL，虚机走 ``pve:manage`` + 虚拟机 ACL。

不真连 SSH：只覆盖目标解析、权限、ACL 与凭据回退这些入口分支。走到真正建连的
用例会因为目标不可达拿到 502，这恰好证明前置校验全部通过。
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.role import Role
from app.models.role_pve_guest_access import RolePveGuestAccess
from app.models.user import User
from app.services.auth import create_access_token
from app.services.containers_collector import pve_target_id
from app.services.crypto import encrypt

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_user(db, permissions: list[str], *, device_scope: str = "all"):
    role = Role(
        name=f"files_role_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        permissions=json.dumps(permissions),
        device_scope=device_scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"filesuser_{role.id}",
        password="irrelevant",
        role="viewer",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    db.commit()
    token = create_access_token(
        user_id=user.id, username=user.username, role=user.role, permissions=permissions
    )
    return role, user, token


def _make_guest(db, *, with_binding: bool = True, os_system: str = "linux"):
    conn = PveConnection(
        name=f"files-pve-{STAMP}",
        host="10.9.0.88",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = None
    if with_binding:
        binding = PveGuestBinding(
            connection_id=conn.id,
            guest_type="qemu",
            vmid=102,
            ip_address="10.9.0.102",
            os_system=os_system,
            username="root",
            password_enc=encrypt("S3cret!pass"),
            enabled=1,
        )
        db.add(binding)
    db.commit()
    return conn, binding


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _cleanup(db, roles, conns):
    for role in roles:
        db.query(RolePveGuestAccess).filter(
            RolePveGuestAccess.role_id == role.id
        ).delete(synchronize_session=False)
        db.query(User).filter(User.role_id == role.id).delete(synchronize_session=False)
        db.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
    for conn in conns:
        db.query(PveGuestBinding).filter(
            PveGuestBinding.connection_id == conn.id
        ).delete(synchronize_session=False)
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete(
            synchronize_session=False
        )
    db.commit()


def test_guest_target_requires_pve_manage_not_device_remote(db):
    """只有 device:remote 的角色不能碰虚拟机文件——虚拟化是独立权限域。"""
    role, _user, token = _make_user(db, ["device:remote", "pve:view"])
    conn, binding = _make_guest(db)
    target_id = pve_target_id(conn.id, binding.vmid)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp"},
            headers=_auth(token),
        )
        assert res.status_code == 403
    finally:
        _cleanup(db, [role], [conn])


def test_guest_without_binding_reports_missing_access(db):
    """没配运维接入的虚机要给出明确原因，而不是笼统的"设备不存在"。"""
    role, _user, token = _make_user(db, ["pve:manage"])
    conn, _binding = _make_guest(db, with_binding=False)
    target_id = pve_target_id(conn.id, 102)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp"},
            headers=_auth(token),
        )
        assert res.status_code == 404
        assert "运维接入" in res.json()["detail"]
    finally:
        _cleanup(db, [role], [conn])


def test_guest_scope_selected_blocks_unlisted_vmid(db):
    """device_scope='selected' 且白名单里没有这台 → 403。"""
    role, _user, token = _make_user(db, ["pve:manage"], device_scope="selected")
    conn, binding = _make_guest(db)
    target_id = pve_target_id(conn.id, binding.vmid)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp"},
            headers=_auth(token),
        )
        assert res.status_code == 403
        assert "虚拟机" in res.json()["detail"]
    finally:
        _cleanup(db, [role], [conn])


def test_guest_scope_selected_allows_listed_vmid(db):
    """白名单命中后前置校验全过，只在真正建连时因目标不可达报 502。"""
    role, _user, token = _make_user(db, ["pve:manage"], device_scope="selected")
    conn, binding = _make_guest(db)
    db.add(
        RolePveGuestAccess(
            role_id=role.id,
            connection_id=conn.id,
            guest_type=binding.guest_type,
            vmid=binding.vmid,
            guest_name="Require",
        )
    )
    db.commit()
    target_id = pve_target_id(conn.id, binding.vmid)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp"},
            headers=_auth(token),
        )
        # 10.9.0.102 不可达 → SSH 建连失败，说明权限/ACL/凭据解析都已通过
        assert res.status_code == 502
        assert "SSH 连接失败" in res.json()["detail"]
    finally:
        _cleanup(db, [role], [conn])


def test_guest_credentials_fall_back_to_binding(db):
    """请求里带空串凭据时，必须回退到 binding 上已存的账号密码。

    前端 FileManager 把未填写的字段发成空串而不是 null，若空串参与"请求值优先"
    判断，就会把已存凭据覆盖成空 → 认证失败。这里断言报错是"连不上"而非"缺少凭据"。
    """
    role, _user, token = _make_user(db, ["pve:manage"])
    conn, binding = _make_guest(db)
    target_id = pve_target_id(conn.id, binding.vmid)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp", "username": "", "password": ""},
            headers=_auth(token),
        )
        assert res.status_code == 502
        assert "缺少 SSH 凭据" not in res.json()["detail"]
    finally:
        _cleanup(db, [role], [conn])


def test_guest_binding_without_username_is_rejected(db):
    """binding 上没有用户名且请求也没给 → 400 明确提示缺凭据。"""
    role, _user, token = _make_user(db, ["pve:manage"])
    conn = PveConnection(
        name=f"files-pve-nouser-{STAMP}",
        host="10.9.0.89",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="lxc",
        vmid=200,
        ip_address="10.9.0.200",
        os_system="linux",
        username=None,
        enabled=1,
    )
    db.add(binding)
    db.commit()
    target_id = pve_target_id(conn.id, binding.vmid)
    try:
        res = client.post(
            f"/api/devices/{target_id}/files/list",
            json={"path": "/tmp"},
            headers=_auth(token),
        )
        assert res.status_code == 400
        assert "缺少 SSH 凭据" in res.json()["detail"]
    finally:
        _cleanup(db, [role], [conn])
