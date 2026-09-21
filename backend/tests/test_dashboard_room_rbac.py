"""dashboard/rooms 的 RBAC 范围一致性(越权审计批次 2)。

越权实测(2026-09 测试报告):device_scope=selected 且未勾任何设备的
device:view 账号,能从 /api/dashboard/overview 拿到全局机房数与用户数,
/api/rooms/tree 返回全部机房拓扑。本文件钉住修复口径:
  * room_count 只统计有授权设备的机房(rack_count 同口径);
  * user_count 仅 user:manage 可见,其它角色为 0;
  * /rooms/tree 与 /dashboard/room-summary 跳过没有授权设备的机房。
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.role_device_access import RoleDeviceAccess
from app.models.room import Room
from app.models.user import User
from app.services.auth import create_access_token

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_role_user(db, permissions: list[str], *, device_scope: str = "all"):
    role = Role(
        name=f"dashrbac_role_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        permissions=json.dumps(permissions),
        device_scope=device_scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"dashrbac_user_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=permissions,
    )
    return role, user, token


def _make_room(db, name: str, with_device: bool):
    room = Room(name=name, location=None)
    db.add(room)
    db.flush()
    if with_device:
        rack = Rack(room_id=room.id, name=f"{name}-rack", type="cabinet")
        db.add(rack)
        db.flush()
        device = Device(
            rack_id=rack.id,
            name=f"{name}-dev",
            type="server",
            ip_address="10.0.0.9",
            status="online",
            ssh_port=22,
            rdp_port=3389,
        )
        db.add(device)
        db.flush()
        db.commit()
        return room, [device]
    db.commit()
    return room, []


def _cleanup(db, room_ids, role_ids):
    db.query(Device).filter(
        Device.rack_id.in_(
            db.query(Rack.id).filter(Rack.room_id.in_(room_ids)).subquery()
        )
    ).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.room_id.in_(room_ids)).delete(synchronize_session=False)
    db.query(Room).filter(Room.id.in_(room_ids)).delete(synchronize_session=False)
    db.query(User).filter(User.role_id.in_(role_ids)).delete(synchronize_session=False)
    db.query(Role).filter(Role.id.in_(role_ids)).delete(synchronize_session=False)
    db.commit()


def test_restricted_user_gets_zero_counts_and_empty_tree(db):
    """selected 空授权的 device:view:机房数 0、用户数 0、树为空、汇总为空。"""
    room_no_dev, _ = _make_room(db, f"rbac-empty-{STAMP}", with_device=False)
    role, _user, token = _make_role_user(db, ["device:view"], device_scope="selected")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/dashboard/overview", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["room_count"] == 0
        assert data["rack_count"] == 0
        assert data["user_count"] == 0

        res = client.get("/api/rooms/tree", headers=headers)
        assert res.status_code == 200
        assert res.json() == []

        res = client.get("/api/dashboard/room-summary", headers=headers)
        assert res.status_code == 200
        assert res.json()["rooms"] == []
    finally:
        _cleanup(db, [room_no_dev.id], [role.id])


def test_restricted_user_sees_only_rooms_with_authorized_devices(db):
    """授权了设备 A 的账号:只看到设备 A 所在机房,看不到空机房。"""
    room_with, devices = _make_room(db, f"rbac-has-{STAMP}", with_device=True)
    room_no_dev, _ = _make_room(db, f"rbac-none-{STAMP}", with_device=False)
    role, _user, token = _make_role_user(db, ["device:view"], device_scope="selected")
    # 给角色授权 devices[0] —— 通过 role_device_access
    db.add(RoleDeviceAccess(role_id=role.id, device_id=devices[0].id))
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/dashboard/overview", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["room_count"] == 1  # 只有含授权设备的机房
        assert data["user_count"] == 0  # 没有 user:manage

        res = client.get("/api/rooms/tree", headers=headers)
        tree = res.json()
        assert [t["id"] for t in tree] == [room_with.id]

        res = client.get("/api/dashboard/room-summary", headers=headers)
        items = res.json()["rooms"]
        assert [i["id"] for i in items] == [room_with.id]
    finally:
        db.query(RoleDeviceAccess).filter(RoleDeviceAccess.role_id == role.id).delete()
        _cleanup(db, [room_with.id, room_no_dev.id], [role.id])


def test_user_manage_sees_user_count(db):
    """user:manage 角色:user_count 返回真实值(测试库至少有本用例建的账号)。"""
    room_no_dev, _ = _make_room(db, f"rbac-empty-{STAMP}", with_device=False)
    role, _user, token = _make_role_user(
        db, ["device:view", "user:manage"], device_scope="selected"
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/dashboard/overview", headers=headers)
        assert res.status_code == 200
        assert res.json()["user_count"] >= 1
    finally:
        _cleanup(db, [room_no_dev.id], [role.id])
