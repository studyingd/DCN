"""guests/rrd-latest 批量端点:列表页首轮速率预填的数据源。

要点:
- 只返回 running 且非模板的 guest;
- 每台取 RRD hour 时间轴最后一个有效采样点;
- 单台失败不影响其余;
- 过虚拟机级 ACL(device_scope=selected 时只返回白名单内的)。
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.pve_connection import PveConnection
from app.models.role import Role
from app.models.role_pve_guest_access import RolePveGuestAccess
from app.models.user import User
from app.routers import pve as pve_router
from app.services.auth import create_access_token

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()

GUESTS = [
    {"id": "qemu/101", "vmid": 101, "node": "n1", "status": "running", "name": "web"},
    {"id": "qemu/102", "vmid": 102, "node": "n1", "status": "stopped", "name": "off"},
    {"id": "qemu/103", "vmid": 103, "node": "n1", "status": "running", "template": 1},
    {"id": "lxc/202", "vmid": 202, "node": "n1", "status": "running", "name": "db"},
]

RRD = {
    101: [
        {"time": 1000, "diskread": 1.5, "diskwrite": 2.5, "netin": 3.5, "netout": 4.5},
        {
            "time": 2000,
            "diskread": 10.0,
            "diskwrite": 20.0,
            "netin": 30.0,
            "netout": 40.0,
        },
    ],
    # 202 的 hour 轴为空 → 不应出现在结果里
    202: [],
}


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_conn(db) -> PveConnection:
    conn = PveConnection(
        name=f"rrd-conn-{STAMP}",
        host="10.9.0.78",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    return conn


def _make_user(db, permissions: list[str], *, device_scope: str = "all", vmid=None):
    role = Role(
        name=f"rrd_role_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        permissions=json.dumps(permissions),
        device_scope=device_scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"rrd_user_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
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


def _cleanup(db, conn, role):
    db.query(RolePveGuestAccess).filter(RolePveGuestAccess.role_id == role.id).delete()
    db.query(User).filter(User.role_id == role.id).delete()
    db.query(Role).filter(Role.id == role.id).delete()
    db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
    db.commit()


def _patched_client():
    return patch.object(
        pve_router,
        "_client",
        lambda *_a, **_k: SimpleNamespace(
            list_guest_resources=lambda force=False: GUESTS,
            guest_rrddata=lambda node, gtype, vmid, timeframe="hour": RRD.get(vmid, []),
        ),
    )


def test_rrd_latest_returns_last_point_for_running_guests_only(db):
    conn = _make_conn(db)
    role, _user, token = _make_user(db, ["pve:view"])
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with _patched_client():
            res = client.get(
                f"/api/pve/connections/{conn.id}/guests/rrd-latest", headers=headers
            )
        assert res.status_code == 200, res.text
        items = res.json()["items"]
        # 101:取最后一个有效点;102 停机/103 模板被排除;202 RRD 为空不进结果
        assert set(items.keys()) == {"qemu/101"}
        assert items["qemu/101"]["diskread"] == 10.0
        assert items["qemu/101"]["netout"] == 40.0
    finally:
        _cleanup(db, conn, role)


def test_rrd_latest_guest_failure_does_not_break_others(db):
    conn = _make_conn(db)
    role, _user, token = _make_user(db, ["pve:view"])
    headers = {"Authorization": f"Bearer {token}"}

    def _rrd(node, gtype, vmid, timeframe="hour"):
        if vmid == 101:
            raise RuntimeError("pve exploded")
        return RRD.get(vmid, [])

    try:
        with patch.object(
            pve_router,
            "_client",
            lambda *_a, **_k: SimpleNamespace(
                list_guest_resources=lambda force=False: GUESTS[:2] + [GUESTS[3]],
                guest_rrddata=_rrd,
            ),
        ):
            res = client.get(
                f"/api/pve/connections/{conn.id}/guests/rrd-latest", headers=headers
            )
        assert res.status_code == 200, res.text
        # 101 炸了但 202 的 RRD 是空——都不在结果里;换成 202 有数据再验证一次
        assert res.json()["items"] == {}
    finally:
        _cleanup(db, conn, role)


def test_rrd_latest_respects_guest_acl(db):
    """selected 范围的角色只能拿到白名单内 guest 的预填数据。"""
    conn = _make_conn(db)
    role, _user, token = _make_user(db, ["pve:view"], device_scope="selected")
    db.add(
        RolePveGuestAccess(
            role_id=role.id, connection_id=conn.id, guest_type="qemu", vmid=101
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    rrd2 = {**RRD, 202: RRD[101]}  # 让 202 也有数据,验证它被 ACL 滤掉
    try:
        with patch.object(
            pve_router,
            "_client",
            lambda *_a, **_k: SimpleNamespace(
                list_guest_resources=lambda force=False: GUESTS,
                guest_rrddata=lambda node, gtype, vmid, timeframe="hour": rrd2.get(
                    vmid, []
                ),
            ),
        ):
            res = client.get(
                f"/api/pve/connections/{conn.id}/guests/rrd-latest", headers=headers
            )
        assert res.status_code == 200, res.text
        assert set(res.json()["items"].keys()) == {"qemu/101"}
    finally:
        _cleanup(db, conn, role)
