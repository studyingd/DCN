"""设备 IP 查重(create/update 拒绝同 IP 双设备)。

直接调路由函数(传 ORM session + 真实 User 行,device_scope=all),不走
TestClient——避免鉴权链与 REPEATABLE READ 快照坑(见 AGENTS.md)。行由
conftest 的 id 快照 janitor 回收,用例内也手动删,双保险。
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.database import SessionLocal
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.routers.devices import create_device, update_device
from app.schemas.device import DeviceCreate, DeviceUpdate


@pytest.fixture
def db_session():
    db = SessionLocal()
    yield db
    db.rollback()
    db.close()


@pytest.fixture
def operator(db_session):
    """device_scope=all 的普通用户,过 user_can_access_device 检查。"""
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"ipuniq_role_{stamp}", permissions='["device:view"]', device_scope="all"
    )
    db_session.add(role)
    db_session.flush()
    user = User(
        username=f"ipuniq_{stamp}",
        password="irrelevant",
        role=f"ipuniq_{stamp}",
        is_active=1,
        role_id=role.id,
    )
    db_session.add(user)
    db_session.flush()
    yield user
    db_session.query(User).filter(User.id == user.id).delete(synchronize_session=False)
    db_session.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
    db_session.commit()


@pytest.fixture
def rack(db_session):
    room = Room(name="IP-Unique Room", location="L1")
    db_session.add(room)
    db_session.flush()
    rack = Rack(room_id=room.id, name="Rack-IP-Unique", type="cabinet", capacity_u=24)
    db_session.add(rack)
    db_session.commit()
    yield rack
    db_session.query(Device).filter(Device.rack_id == rack.id).delete(
        synchronize_session=False
    )
    db_session.query(Rack).filter(Rack.id == rack.id).delete(synchronize_session=False)
    db_session.query(Room).filter(Room.id == room.id).delete(synchronize_session=False)
    db_session.commit()


def _create(db, rack, name: str, ip: str, user: User, **extra) -> Device:
    body = DeviceCreate(name=name, type="server", ip_address=ip, **extra)
    return create_device(rack_id=rack.id, body=body, db=db, current_user=user)


def test_create_rejects_duplicate_ip(db_session, rack, operator):
    first = _create(db_session, rack, "ip-unique-a", "10.99.0.101", operator)
    assert first.ip_address == "10.99.0.101"

    with pytest.raises(HTTPException) as exc:
        _create(db_session, rack, "ip-unique-b", "10.99.0.101", operator)
    assert exc.value.status_code == 422
    assert "10.99.0.101" in exc.value.detail


def test_create_allows_unique_and_empty_ip(db_session, rack, operator):
    _create(db_session, rack, "ip-unique-c", "10.99.0.102", operator)
    # 空 IP 放行(shelf/未接入设备)
    empty = _create(db_session, rack, "ip-unique-empty", None, operator)
    assert empty.ip_address is None


def test_update_rejects_other_device_ip(db_session, rack, operator):
    _create(db_session, rack, "ip-unique-d", "10.99.0.103", operator)
    victim = _create(db_session, rack, "ip-unique-e", "10.99.0.104", operator)

    with pytest.raises(HTTPException) as exc:
        update_device(
            device_id=victim.id,
            body=DeviceUpdate(ip_address="10.99.0.103"),
            db=db_session,
            current_user=operator,
        )
    assert exc.value.status_code == 422


def test_update_allows_keeping_own_ip(db_session, rack, operator):
    device = _create(db_session, rack, "ip-unique-f", "10.99.0.105", operator)

    result = update_device(
        device_id=device.id,
        body=DeviceUpdate(name="ip-unique-f-renamed", ip_address="10.99.0.105"),
        db=db_session,
        current_user=operator,
    )
    assert result.ip_address == "10.99.0.105"
    assert result.name == "ip-unique-f-renamed"
