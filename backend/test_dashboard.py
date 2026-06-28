"""Tests for the big-screen dashboard API endpoints."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.audit_log import AuditLog
from app.models.connection import Connection
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
        name="Switch-1",
        type="switch",
        ip_address="10.0.0.2",
        status="offline",
        ssh_port=22,
        rdp_port=3389,
    )
    db.add_all([d1, d2])
    db.flush()

    conn = Connection(
        device_a_id=d1.id, device_b_id=d2.id, conn_type="ethernet", bandwidth="1Gbps"
    )
    db.add(conn)

    # Add an audit log entry
    audit = AuditLog(
        event_type="system_login",
        username=user.username,
        user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(audit)
    db.commit()

    mock_statuses = {d1.id: "online", d2.id: "offline"}

    try:
        yield db, token, mock_statuses
    finally:
        # Cleanup
        db.query(AuditLog).filter(AuditLog.username == user.username).delete()
        db.query(Connection).filter(Connection.id == conn.id).delete()
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
    assert "connection_count" in data
    assert "user_count" in data
    assert "online_user_count" in data
    assert data["device_total"] >= 2
    assert data["room_count"] >= 1


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
    # Should have at least server and switch entries
    types_found = {item["type"] for item in dist}
    assert "server" in types_found or "switch" in types_found
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


def test_connection_topology(setup_data):
    """GET /api/dashboard/connection-topology returns nodes and edges."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        resp = client.get(
            "/api/dashboard/connection-topology", headers=_auth_headers(token)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) >= 2
    assert len(data["edges"]) >= 1
    for node in data["nodes"]:
        assert "id" in node
        assert "name" in node
        assert "type" in node
        assert "ip" in node
        assert "status" in node
    for edge in data["edges"]:
        assert "source" in edge
        assert "target" in edge
        assert "conn_type" in edge


def test_audit_timeline(setup_data):
    """GET /api/dashboard/audit-timeline returns events and counts."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        resp = client.get("/api/dashboard/audit-timeline", headers=_auth_headers(token))

    assert resp.status_code == 200
    data = resp.json()
    assert "recent_events" in data
    assert "event_counts_today" in data
    assert isinstance(data["recent_events"], list)
    assert isinstance(data["event_counts_today"], dict)
    for evt in data["recent_events"]:
        assert "id" in evt
        assert "event_type" in evt
        assert "username" in evt
        assert "device_name" in evt
        assert "command" in evt
        assert "created_at" in evt


def test_login_trend(setup_data):
    """GET /api/dashboard/login-trend returns daily login counts."""
    db, token, mock_statuses = setup_data
    with patch("app.routers.dashboard.get_latest_statuses", return_value=mock_statuses):
        resp = client.get(
            "/api/dashboard/login-trend?days=7", headers=_auth_headers(token)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "trend" in data
    trend = data["trend"]
    assert isinstance(trend, list)
    assert len(trend) == 7
    for item in trend:
        assert "date" in item
        assert "login_count" in item
        assert isinstance(item["login_count"], int)


def test_unauthenticated_returns_401():
    """Endpoints should return 401 without a valid token."""
    endpoints = [
        "/api/dashboard/overview",
        "/api/dashboard/device-type-distribution",
        "/api/dashboard/room-summary",
        "/api/dashboard/connection-topology",
        "/api/dashboard/audit-timeline",
        "/api/dashboard/login-trend",
    ]
    for url in endpoints:
        resp = client.get(url)
        assert resp.status_code == 401, f"{url} should return 401 without auth"
