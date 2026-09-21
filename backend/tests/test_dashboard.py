"""Tests for the big-screen dashboard API endpoints."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.alert import AlertEvent, AlertRule
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services.auth import create_access_token

client = TestClient(app)


# ── Fixtures ──


def _create_role_and_user(db: Session) -> tuple[Role, User, str]:
    """Create a role with device:view permission and an active user, return (role, user, token)."""
    role = Role(
        name="test_dashboard_role",
        permissions='["device:view"]',
        device_scope="all",
    )
    db.add(role)
    db.flush()

    user = User(
        username=f"dashuser_{datetime.now(timezone.utc).timestamp()}",
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
        permissions=["device:view"],
        device_scope="all",
    )
    return role, user, token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def setup_data():
    """Insert test data and return (db, token). Cleanup after test."""
    db = SessionLocal()
    # Create tables if needed
    Base.metadata.create_all(bind=engine)

    role, user, token = _create_role_and_user(db)

    room = Room(name="Test Room", location="Floor 1")
    db.add(room)
    db.flush()

    rack = Rack(room_id=room.id, name="Rack-1", type="cabinet")
    db.add(rack)
    db.flush()

    d1 = Device(
        rack_id=rack.id,
        name="Server-1",
        type="server",
        ip_address="10.0.0.1",
        status="online",
        ssh_port=22,
        rdp_port=3389,
    )
    d2 = Device(
        rack_id=rack.id,
        name="Cloud-1",
        type="cloud_server",
        ip_address="10.0.0.2",
        status="offline",
        ssh_port=22,
        rdp_port=3389,
    )
    db.add_all([d1, d2])
    db.flush()

    db.commit()

    mock_statuses = {d1.id: "online", d2.id: "offline"}

    try:
        yield db, token, mock_statuses
    finally:
        # Cleanup
        db.query(Device).filter(Device.id.in_([d1.id, d2.id])).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


# ── Tests ──


def test_overview(setup_data):
    """GET /api/dashboard/overview returns expected structure."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        with patch("app.routers.dashboard.get_online_users", return_value=[]):
            resp = client.get("/api/dashboard/overview", headers=_auth_headers(token))

    assert resp.status_code == 200
    data = resp.json()
    assert "room_count" in data
    assert "rack_count" in data
    assert "device_total" in data
    assert "device_online" in data
    assert "device_offline" in data
    assert "device_maintenance" in data
    assert "connection_count" not in data
    assert "user_count" in data
    assert "online_user_count" in data
    assert data["device_total"] >= 2
    assert data["room_count"] >= 1


def test_overview_alert_count_7d(setup_data):
    """GET /api/dashboard/overview 的 alert_events_7d 只统计近 7 天触发的告警事件。"""
    db, token, mock_statuses = setup_data

    def fetch_count() -> int:
        with patch(
            "app.routers.dashboard.get_latest_statuses", return_value=mock_statuses
        ):
            with patch("app.routers.dashboard.get_online_users", return_value=[]):
                resp = client.get(
                    "/api/dashboard/overview", headers=_auth_headers(token)
                )
        assert resp.status_code == 200
        return resp.json()["alert_events_7d"]

    baseline = fetch_count()

    rule = AlertRule(
        name="dash_alert_rule",
        metric="cpu_pct",
        operator="gt",
        threshold=90.0,
        severity="warning",
    )
    db.add(rule)
    db.flush()
    now = datetime.now(timezone.utc)
    recent = AlertEvent(
        rule_id=rule.id,
        metric="cpu_pct",
        value=95.0,
        threshold=90.0,
        severity="warning",
        message="recent",
        first_triggered_at=now - timedelta(hours=2),
        last_seen_at=now,
    )
    stale = AlertEvent(
        rule_id=rule.id,
        metric="cpu_pct",
        value=99.0,
        threshold=90.0,
        severity="warning",
        message="stale",
        first_triggered_at=now - timedelta(days=10),
        last_seen_at=now - timedelta(days=10),
    )
    db.add_all([recent, stale])
    db.commit()
    try:
        assert fetch_count() == baseline + 1
    finally:
        db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).delete()
        db.query(AlertRule).filter(AlertRule.id == rule.id).delete()
        db.commit()


def test_device_type_distribution(setup_data):
    """GET /api/dashboard/device-type-distribution returns per-type breakdown."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        resp = client.get(
            "/api/dashboard/device-type-distribution", headers=_auth_headers(token)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "distribution" in data
    dist = data["distribution"]
    assert isinstance(dist, list)
    # fixture 建了 server 与 cloud_server 各一台,两种类型都必须出现在分布里
    types_found = {item["type"] for item in dist}
    assert {"server", "cloud_server"} <= types_found
    for item in dist:
        assert "type" in item
        assert "label" in item
        assert "count" in item
        assert "online" in item
        assert "offline" in item
        assert "maintenance" in item


def test_room_summary(setup_data):
    """GET /api/dashboard/room-summary returns per-room stats."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        resp = client.get("/api/dashboard/room-summary", headers=_auth_headers(token))

    assert resp.status_code == 200
    data = resp.json()
    assert "rooms" in data
    rooms = data["rooms"]
    assert isinstance(rooms, list)
    assert len(rooms) >= 1
    for room in rooms:
        assert "id" in room
        assert "name" in room
        assert "location" in room
        assert "rack_count" in room
        assert "device_count" in room
        assert "device_online" in room
        assert "device_offline" in room
        assert "device_maintenance" in room


def test_removed_connection_endpoints_return_404(setup_data):
    """The removed device connection/topology module has no public routes."""
    _db, token, _mock_statuses = setup_data
    for url in ("/api/connections", "/api/dashboard/connection-topology"):
        resp = client.get(url, headers=_auth_headers(token))
        assert resp.status_code == 404


def test_unauthenticated_returns_401():
    """Endpoints should return 401 without a valid token."""
    endpoints = [
        "/api/dashboard/overview",
        "/api/dashboard/device-type-distribution",
        "/api/dashboard/room-summary",
    ]
    for url in endpoints:
        resp = client.get(url)
        assert resp.status_code == 401, f"{url} should return 401 without auth"
