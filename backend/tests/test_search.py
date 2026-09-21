"""顶栏全局搜索端点(/api/search)测试。

设备部分走真实 DB(自建房间/机柜/设备,断言只限定自己的行);虚机部分
mock 候选采集器与最近已知 IP,避免依赖开发库里的真实 PVE 连接——
`collect_pve_guest_candidates` 在有启用连接时会触达真实 PVE API。
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services.auth import create_access_token

client = TestClient(app)


def _unique(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).timestamp()}"


def _mk_role_user(db: Session, permissions: list[str]) -> tuple[Role, User, str]:
    role = Role(
        name=_unique("search_role"),
        permissions=json.dumps(permissions),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=_unique("searchuser"),
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


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def device_data():
    """一对设备挂在自建机房/机柜下;测试结束精确清理本 fixture 创建的行。"""
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    role, user, token = _mk_role_user(db, ["device:view", "pve:view"])

    room = Room(name=_unique("search_room"), location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=_unique("search_rack"), type="cabinet")
    db.add(rack)
    db.flush()
    d1 = Device(
        rack_id=rack.id,
        name=_unique("search_dev"),
        type="server",
        ip_address="10.99.0.1",
        status="online",
        ssh_port=22,
        rdp_port=3389,
    )
    d2 = Device(
        rack_id=rack.id,
        name=_unique("search_dev"),
        type="cloud_server",
        ip_address="10.99.0.2",
        status="offline",
        ssh_port=22,
        rdp_port=3389,
    )
    db.add_all([d1, d2])
    db.commit()

    try:
        yield db, token, room, rack, d1, d2
    finally:
        db.query(Device).filter(Device.id.in_([d1.id, d2.id])).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


# ── 设备搜索 ──


def test_search_device_by_name(device_data):
    db, token, room, rack, d1, d2 = device_data
    resp = client.get("/api/search", params={"q": d1.name}, headers=_headers(token))
    assert resp.status_code == 200
    items = resp.json()["devices"]
    mine = [i for i in items if i["id"] == d1.id]
    assert len(mine) == 1
    assert mine[0]["name"] == d1.name
    assert mine[0]["room_id"] == room.id
    assert mine[0]["room_name"] == room.name
    assert mine[0]["rack_id"] == rack.id
    assert mine[0]["rack_name"] == rack.name


def test_search_device_by_ip(device_data):
    db, token, room, rack, d1, d2 = device_data
    resp = client.get("/api/search", params={"q": "10.99.0.2"}, headers=_headers(token))
    assert resp.status_code == 200
    items = resp.json()["devices"]
    assert [i["id"] for i in items if i["id"] == d2.id] == [d2.id]
    assert [i["id"] for i in items if i["id"] == d1.id] == []  # 另一台不匹配


def test_search_requires_auth():
    resp = client.get("/api/search", params={"q": "x"})
    assert resp.status_code == 401


def test_search_blank_keyword_returns_empty(device_data):
    db, token, *_ = device_data
    resp = client.get("/api/search", params={"q": "   "}, headers=_headers(token))
    assert resp.status_code == 200
    assert resp.json() == {"devices": [], "guests": []}


def test_search_device_view_gate():
    """无 device:view 的角色搜不到设备(虚机域不受影响,另行验证)。"""
    db = SessionLocal()
    role, user, token = _mk_role_user(db, ["pve:view"])
    db.commit()
    try:
        resp = client.get("/api/search", params={"q": "10.99"}, headers=_headers(token))
        assert resp.status_code == 200
        assert resp.json()["devices"] == []
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


# ── 虚机搜索 ──


def _mock_candidates():
    return [
        {
            "connection_id": 7,
            "connection_name": "pve-test",
            "guest_type": "qemu",
            "vmid": 101,
            "name": "web-01",
            "node": "node1",
            "status": "running",
            "ip_address": "192.168.10.11",
            "os_system": "linux",
            "cpu": 0.1,
            "mem": 1,
            "maxmem": 4,
            "reachable": 1,
        },
        {
            "connection_id": 7,
            "connection_name": "pve-test",
            "guest_type": "qemu",
            "vmid": 102,
            "name": "db-02",
            "node": "node1",
            "status": "stopped",
            "ip_address": None,
            "os_system": None,
            "cpu": 0,
            "mem": 0,
            "maxmem": 8,
            "reachable": 1,
        },
    ]


def _with_guest_mocks():
    def _last_ip(_conn_id: int, vmid: int) -> str | None:
        return {101: "192.168.10.99", 102: "192.168.20.50"}.get(vmid)

    return (
        patch(
            "app.routers.search.collect_pve_guest_candidates",
            return_value=_mock_candidates(),
        ),
        patch("app.routers.search.get_last_known_ip", side_effect=_last_ip),
    )


def test_search_guest_by_name(device_data):
    db, token, *_ = device_data
    m1, m2 = _with_guest_mocks()
    with m1, m2:
        resp = client.get("/api/search", params={"q": "web"}, headers=_headers(token))
    assert resp.status_code == 200
    guests = resp.json()["guests"]
    assert [(g["vmid"], g["name"]) for g in guests] == [(101, "web-01")]
    assert guests[0]["ip_address"] == "192.168.10.11"
    assert guests[0]["last_known_ip"] == "192.168.10.99"


def test_search_guest_by_last_known_ip(device_data):
    """无绑定 IP 的虚机按最近已知 IP 也能搜到。"""
    db, token, *_ = device_data
    m1, m2 = _with_guest_mocks()
    with m1, m2:
        resp = client.get(
            "/api/search", params={"q": "192.168.20.50"}, headers=_headers(token)
        )
    assert resp.status_code == 200
    assert [g["vmid"] for g in resp.json()["guests"]] == [102]


def test_search_guest_no_match(device_data):
    db, token, *_ = device_data
    m1, m2 = _with_guest_mocks()
    with m1, m2:
        resp = client.get(
            "/api/search", params={"q": "no-such-thing"}, headers=_headers(token)
        )
    assert resp.status_code == 200
    assert resp.json()["guests"] == []


def test_search_guest_pve_view_gate():
    """无 pve:view 的角色搜不到虚机。"""
    db = SessionLocal()
    role, user, token = _mk_role_user(db, ["device:view"])
    db.commit()
    try:
        m1, m2 = _with_guest_mocks()
        with m1, m2:
            resp = client.get(
                "/api/search", params={"q": "web"}, headers=_headers(token)
            )
        assert resp.status_code == 200
        assert resp.json()["guests"] == []
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()
