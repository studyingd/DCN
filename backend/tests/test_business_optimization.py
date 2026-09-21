from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.business import Business, BusinessInterface, BusinessServer
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.service_interface import ServiceInterface
from app.models.user import User
from app.services.auth import create_access_token

client = TestClient(app)


def test_create_interface_and_link_is_atomic_and_returns_detail():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    role = Role(
        name=f"business_role_{suffix}",
        permissions='["device:view","device:manage"]',
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"business_user_{suffix}",
        password="irrelevant",
        role="operator",
        is_active=1,
        role_id=role.id,
    )
    business = Business(name=f"business_{suffix}")
    db.add_all([user, business])
    db.commit()

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["device:view", "device:manage"],
        device_scope="all",
    )
    interface_name = f"health_{suffix}"
    response = client.post(
        f"/api/businesses/{business.id}/interfaces",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": interface_name,
            "url": "https://example.com/health",
            "method": "GET",
            "expected_status": 200,
            "timeout": 5,
            "enabled": 1,
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["interface_total"] == 1
    assert data["interfaces"][0]["name"] == interface_name
    db.rollback()
    assert (
        db.query(ServiceInterface)
        .filter(ServiceInterface.name == interface_name)
        .count()
        == 1
    )
    assert (
        db.query(BusinessInterface)
        .filter(BusinessInterface.business_id == business.id)
        .count()
        == 1
    )

    interface_ids = [
        row.id
        for row in db.query(ServiceInterface)
        .filter(ServiceInterface.name == interface_name)
        .all()
    ]
    db.query(BusinessInterface).filter(
        BusinessInterface.business_id == business.id
    ).delete(synchronize_session=False)
    if interface_ids:
        db.query(ServiceInterface).filter(
            ServiceInterface.id.in_(interface_ids)
        ).delete(synchronize_session=False)
    db.delete(business)
    db.delete(user)
    db.delete(role)
    db.commit()
    db.close()


def test_batch_server_link_uses_one_request_and_returns_detail():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    role = Role(
        name=f"batch_role_{suffix}",
        permissions='["device:view","device:manage"]',
        device_scope="all",
    )
    room = Room(name=f"batch_room_{suffix}")
    db.add_all([role, room])
    db.flush()
    rack = Rack(room_id=room.id, name=f"batch_rack_{suffix}", type="cabinet")
    user = User(
        username=f"batch_user_{suffix}",
        password="irrelevant",
        role="operator",
        is_active=1,
        role_id=role.id,
    )
    business = Business(name=f"batch_business_{suffix}")
    db.add_all([rack, user, business])
    db.flush()
    devices = [
        Device(
            rack_id=rack.id,
            name=f"server_{suffix}_{index}",
            type="server",
            ip_address=f"192.0.2.{index + 10}",
            status="online",
        )
        for index in range(2)
    ]
    db.add_all(devices)
    db.commit()

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["device:view", "device:manage"],
        device_scope="all",
    )
    response = client.post(
        f"/api/businesses/{business.id}/servers/batch",
        headers={"Authorization": f"Bearer {token}"},
        json={"ids": [device.id for device in devices]},
    )

    assert response.status_code == 200
    assert response.json()["server_total"] == 2
    db.rollback()
    assert (
        db.query(BusinessServer)
        .filter(BusinessServer.business_id == business.id)
        .count()
        == 2
    )

    db.query(BusinessServer).filter(BusinessServer.business_id == business.id).delete(
        synchronize_session=False
    )
    for device in devices:
        db.delete(device)
    db.delete(business)
    db.delete(user)
    db.delete(rack)
    db.delete(room)
    db.delete(role)
    db.commit()
    db.close()
