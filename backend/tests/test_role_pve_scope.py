"""角色权限收敛（B）与虚拟机级 ACL（A）的回归测试。

覆盖三件事：
1. 权限目录已从 12 项收敛到 8 项，且按 5 个分组返回给前端；
2. 角色行里的历史权限键仍能被归一到新键（未保存过的旧角色不至于直接失权）；
3. ``device_scope='selected'`` 时，虚拟机候选/业务关联只暴露白名单内的 guest，
   ``device_scope='all'``（默认）时行为与改动前一致。虚拟机与设备共用同一个
   范围开关，不再单独维护 ``pve_scope``。
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
from app.services.auth import create_access_token
from app.services.permissions import (
    PERMISSIONS,
    get_user_permissions,
    get_user_pve_guest_keys,
)

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()


def _make_role_user(db, permissions: list[str], *, device_scope: str = "all") -> Role:
    role = Role(
        name=f"rbac_role_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        permissions=json.dumps(permissions),
        device_scope=device_scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"rbacuser_{role.id}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=permissions,
    )
    role._test_user = user  # type: ignore[attr-defined]
    role._test_token = token  # type: ignore[attr-defined]
    return role


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _cleanup(db, role: Role):
    db.query(RolePveGuestAccess).filter(RolePveGuestAccess.role_id == role.id).delete(
        synchronize_session=False
    )
    db.query(User).filter(User.role_id == role.id).delete(synchronize_session=False)
    db.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
    db.commit()


# ── B. 权限目录 ────────────────────────────────────────────────


def test_permission_catalog_is_converged(db):
    """8 个权限键、5 个分组，旧键不再出现在目录里。"""
    role = _make_role_user(db, ["user:manage"])
    try:
        res = client.get(
            "/api/permissions",
            headers={"Authorization": f"Bearer {role._test_token}"},  # type: ignore[attr-defined]
        )
        assert res.status_code == 200
        data = res.json()
        keys = [item["key"] for item in data["permissions"]]
        assert set(keys) == set(PERMISSIONS)
        assert len(keys) == 8
        assert len(data["groups"]) == 5
        # 分组必须无遗漏、无重复地覆盖整个目录
        grouped = [k for g in data["groups"] for k in g["permissions"]]
        assert sorted(grouped) == sorted(keys)
        for retired in (
            "script:manage",
            "inspection:manage",
            "alert:manage",
            "agent:use",
            "docker:control",
        ):
            assert retired not in keys
    finally:
        _cleanup(db, role)


def test_legacy_permission_keys_are_normalized(db):
    """旧角色行里的下线键按映射归一，去重且保序。"""
    role = Role(
        name=f"legacy_role_{STAMP}",
        permissions=json.dumps(
            [
                "script:manage",
                "alert:manage",
                "inspection:manage",
                "agent:use",
                "docker:control",
                "device:view",
                "credential:manage",
            ]
        ),
        device_scope="all",
    )
    db.add(role)
    db.commit()
    try:
        user = db.query(User).filter(User.role_id == role.id).first()
        if user is None:
            user = User(
                username=f"legacyuser_{STAMP}",
                password="x",
                role="viewer",
                is_active=1,
                role_id=role.id,
            )
            db.add(user)
            db.commit()
        assert get_user_permissions(user, db) == [
            "automation:manage",
            "device:remote",
            "device:manage",
            "pve:manage",
            "device:view",
        ]
    finally:
        db.query(User).filter(User.role_id == role.id).delete(synchronize_session=False)
        db.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
        db.commit()


# ── A. 虚拟机级 ACL ────────────────────────────────────────────


def _make_conn(db) -> PveConnection:
    conn = PveConnection(
        name=f"rbac-pve-{STAMP}",
        host="10.9.0.77",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    return conn


GUESTS = {
    101: {"vmid": 101, "guest_type": "qemu", "name": "web-01", "status": "running"},
    202: {"vmid": 202, "guest_type": "lxc", "name": "db-01", "status": "stopped"},
}


def _patched_snapshot(conn_id: int):
    """把内存快照替换成固定的两台 guest，避免依赖真实 PVE。

    候选接口会遍历所有 PveConnection 行(测试库=开发库,可能有其它连接),
    非本测试创建的连接一律返回空,否则同一组 GUESTS 会被重复列出。
    """
    return patch.multiple(
        "app.services.pve_guest_candidates",
        ensure_fresh=lambda *a, **k: None,
        get_connection_state=lambda _id: {"reachable": True},
        get_guests_by_connection=lambda _id: dict(GUESTS) if _id == conn_id else {},
    )


def test_role_crud_persists_pve_guest_whitelist(db):
    """角色接口能保存/回读虚拟机白名单，且与设备共用 device_scope。"""
    admin = _make_role_user(db, ["user:manage"])
    conn = _make_conn(db)
    headers = {"Authorization": f"Bearer {admin._test_token}"}  # type: ignore[attr-defined]
    created_id = None
    try:
        payload = {
            "name": f"vm_scope_role_{STAMP}",
            "permissions": ["device:view", "pve:view"],
            "device_scope": "selected",
            "device_ids": [],
            "pve_guests": [
                {
                    "connection_id": conn.id,
                    "guest_type": "qemu",
                    "vmid": 101,
                    "guest_name": "web-01",
                }
            ],
        }
        res = client.post("/api/roles", json=payload, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        created_id = body["id"]
        assert body["device_scope"] == "selected"
        assert [g["vmid"] for g in body["pve_guests"]] == [101]

        # 更新：换成另一台 guest
        res = client.put(
            f"/api/roles/{created_id}",
            json={
                "pve_guests": [
                    {
                        "connection_id": conn.id,
                        "guest_type": "lxc",
                        "vmid": 202,
                        "guest_name": "db-01",
                    }
                ]
            },
            headers=headers,
        )
        assert res.status_code == 200
        assert [g["vmid"] for g in res.json()["pve_guests"]] == [202]

        res = client.get(f"/api/roles/{created_id}", headers=headers)
        assert [g["guest_type"] for g in res.json()["pve_guests"]] == ["lxc"]
    finally:
        if created_id:
            client.delete(f"/api/roles/{created_id}", headers=headers)
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, admin)


def test_device_scope_selected_limits_visible_guests(db):
    """指定虚拟机范围后，候选接口只返回白名单内的 guest。"""
    conn = _make_conn(db)
    role = _make_role_user(db, ["device:view", "pve:view"], device_scope="selected")
    db.add(
        RolePveGuestAccess(
            role_id=role.id,
            connection_id=conn.id,
            guest_type="qemu",
            vmid=101,
            guest_name="web-01",
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {role._test_token}"}  # type: ignore[attr-defined]
    try:
        assert get_user_pve_guest_keys(role._test_user, db) == {  # type: ignore[attr-defined]
            (conn.id, "qemu", 101)
        }
        with _patched_snapshot(conn.id):
            res = client.get("/api/businesses/pve-guest-candidates", headers=headers)
        assert res.status_code == 200
        assert [item["vmid"] for item in res.json()] == [101]
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, role)


def test_device_scope_all_keeps_every_guest_visible(db):
    """默认 all 时不过滤——既有角色的行为保持不变。"""
    conn = _make_conn(db)
    role = _make_role_user(db, ["device:view", "pve:view"])
    headers = {"Authorization": f"Bearer {role._test_token}"}  # type: ignore[attr-defined]
    try:
        assert get_user_pve_guest_keys(role._test_user, db) is None  # type: ignore[attr-defined]
        with _patched_snapshot(conn.id):
            res = client.get("/api/businesses/pve-guest-candidates", headers=headers)
        assert sorted(item["vmid"] for item in res.json()) == [101, 202]
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, role)


def test_role_candidates_endpoint_ignores_caller_acl(db):
    """角色编辑器的候选接口必须看到全量 guest，否则无法完成授权。"""
    conn = _make_conn(db)
    admin = _make_role_user(db, ["user:manage"], device_scope="selected")
    headers = {"Authorization": f"Bearer {admin._test_token}"}  # type: ignore[attr-defined]
    try:
        with _patched_snapshot(conn.id):
            res = client.get("/api/roles/pve-guest-candidates", headers=headers)
        assert res.status_code == 200
        assert sorted(item["vmid"] for item in res.json()) == [101, 202]
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, admin)


def test_profile_exposes_pve_guests(db):
    """/api/auth/profile 要带上已授权虚拟机，前端据此隐藏未授权入口。"""
    conn = _make_conn(db)
    role = _make_role_user(db, ["device:view", "pve:view"], device_scope="selected")
    db.add(
        RolePveGuestAccess(
            role_id=role.id, connection_id=conn.id, guest_type="qemu", vmid=101
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {role._test_token}"}  # type: ignore[attr-defined]
    try:
        res = client.get("/api/auth/profile", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["device_scope"] == "selected"
        assert body["pve_guests"] == [f"{conn.id}:qemu:101"]
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, role)


def test_pve_overview_filters_guests_by_acl(db):
    """总览接口只返回白名单内的 guest，避免面板上仍能看到未授权的虚机。"""
    from app.routers import pve as pve_router

    conn = _make_conn(db)
    role = _make_role_user(db, ["pve:view"], device_scope="selected")
    db.add(
        RolePveGuestAccess(
            role_id=role.id,
            connection_id=conn.id,
            guest_type="qemu",
            vmid=101,
            guest_name="web-01",
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {role._test_token}"}  # type: ignore[attr-defined]
    raw = [
        {"id": "qemu/101", "vmid": 101, "node": "n1", "status": "running"},
        {"id": "lxc/202", "vmid": 202, "node": "n1", "status": "stopped"},
    ]
    try:
        with (
            patch.object(
                pve_router,
                "_client",
                lambda *_a, **_k: SimpleNamespace(
                    list_nodes=lambda: [{"node": "n1"}],
                    list_guest_resources=lambda force=False: raw,
                ),
            ),
        ):
            res = client.get(
                f"/api/pve/connections/{conn.id}/overview", headers=headers
            )
        assert res.status_code == 200, res.text
        # 202 是 lxc:总览只呈现 qemu(LXC 已移除),白名单内的是 101
        assert [g["vmid"] for g in res.json()["guests"]] == [101]
        assert [n["node"] for n in res.json()["nodes"]] == ["n1"]
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.commit()
        _cleanup(db, role)
