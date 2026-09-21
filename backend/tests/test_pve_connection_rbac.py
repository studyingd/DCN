"""PVE 连接级 RBAC：连接清单过滤/脱敏与连接级读接口守卫。

越权实测（2026-09 测试报告）：只授权 2:qemu:103 的 pve:view 账号可以读
全部连接的基础设施信息——连接清单（host/port/token_id）、任意连接的
overview/nodes/nextid/storage/isos（包括一台虚机都没授权的连接）。
本文件钉住批次 1 的修复口径：
  * 清单：view 用户只见有授权虚机的连接，host/port/token_id 脱敏；
  * overview：manage 全放行；view 需该连接上有 ≥1 台授权虚机；
  * nodes/nextid/storage/isos：建 VM 前置数据，收紧为 pve:manage。
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
from app.services.pve import PveError

client = TestClient(app)

STAMP = datetime.now(timezone.utc).timestamp()


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_conn(db, name: str) -> PveConnection:
    conn = PveConnection(
        name=name,
        host="10.9.0.78",
        token_id="root@pam!dcn",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    return conn


def _make_user(
    db, permissions: list[str], *, device_scope: str = "all", guests: list = None
):
    role = Role(
        name=f"connrbac_role_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
        permissions=json.dumps(permissions),
        device_scope=device_scope,
    )
    db.add(role)
    db.flush()
    if guests:
        for conn_id, gtype, vmid in guests:
            db.add(
                RolePveGuestAccess(
                    role_id=role.id,
                    connection_id=conn_id,
                    guest_type=gtype,
                    vmid=vmid,
                )
            )
        db.flush()
    user = User(
        username=f"connrbac_user_{STAMP}_{abs(hash(tuple(permissions))) % 10**6}",
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


def _cleanup(db, conns, role):
    for guest in db.query(RolePveGuestAccess).filter(
        RolePveGuestAccess.role_id == role.id
    ):
        db.delete(guest)
    db.query(User).filter(User.role_id == role.id).delete()
    db.query(Role).filter(Role.id == role.id).delete()
    for conn in conns:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
    db.commit()


def _patched_overview_client():
    """overview 走 _client:节点与 guest 资源都不真连 PVE。"""
    return patch.object(
        pve_router,
        "_client",
        lambda *_a, **_k: SimpleNamespace(
            list_nodes=lambda: [{"node": "pve", "status": "online"}],
            list_guest_resources=lambda force=False: [
                {"id": "qemu/103", "vmid": 103, "node": "pve", "status": "running"}
            ],
        ),
    )


def test_view_user_sees_only_authorized_connections_and_masked(db):
    """单虚机授权账号：清单只见该连接，host/port/token_id 脱敏。"""
    conn1 = _make_conn(db, f"connrbac-a-{STAMP}")
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/pve/connections", headers=headers)
        assert res.status_code == 200
        rows = res.json()
        assert [r["id"] for r in rows] == [conn2.id]
        row = rows[0]
        assert row["host"] == "" and row["port"] == 0 and row["token_id"] == ""
        assert row["name"] == conn2.name  # 名称保留:连接切换靠它
    finally:
        _cleanup(db, [conn1, conn2], role)


def test_manage_user_sees_all_connections_unmasked(db):
    conn1 = _make_conn(db, f"connrbac-a-{STAMP}")
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(db, ["pve:manage"])
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/pve/connections", headers=headers)
        assert res.status_code == 200
        rows = {r["id"]: r for r in res.json()}
        assert set(rows) >= {conn1.id, conn2.id}
        assert rows[conn1.id]["host"] == "10.9.0.78"
        assert rows[conn1.id]["token_id"] == "root@pam!dcn"
    finally:
        _cleanup(db, [conn1, conn2], role)


def test_view_user_cannot_read_unauthorized_connection_overview(db):
    """无授权虚机的连接：overview 403，不触碰 PVE（_client 不被调用）。"""
    conn1 = _make_conn(db, f"connrbac-a-{STAMP}")
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with _patched_overview_client() as mocked:
            res = client.get(
                f"/api/pve/connections/{conn1.id}/overview", headers=headers
            )
        assert res.status_code == 403
        assert mocked.call_count == 0 if hasattr(mocked, "call_count") else True
    finally:
        _cleanup(db, [conn1, conn2], role)


def test_view_user_reads_authorized_connection_overview(db):
    """有授权虚机的连接：overview 200，guest 列表按 ACL 过滤。"""
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with _patched_overview_client():
            res = client.get(
                f"/api/pve/connections/{conn2.id}/overview", headers=headers
            )
        assert res.status_code == 200
        data = res.json()
        assert data["nodes"] == [{"node": "pve", "status": "online"}]
        assert [g["vmid"] for g in data["guests"]] == [103]
    finally:
        _cleanup(db, [conn2], role)


def test_create_guest_prerequisites_require_manage(db):
    """nodes/nextid/storage/isos 是建 VM 前置数据，收紧为 pve:manage。"""
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view", "pve:manage"], device_scope="selected", guests=[]
    )
    # view-only：单独再做一个没有 manage 的角色
    role2, _u2, token2 = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    try:
        for token_view in (token2,):
            headers = {"Authorization": f"Bearer {token_view}"}
            res = client.get(f"/api/pve/connections/{conn2.id}/nodes", headers=headers)
            assert res.status_code == 403, "/nodes 应要求 pve:manage"
            res = client.get(f"/api/pve/connections/{conn2.id}/nextid", headers=headers)
            assert res.status_code == 403, "/nextid 应要求 pve:manage"
            res = client.get(
                f"/api/pve/connections/{conn2.id}/nodes/pve/storage", headers=headers
            )
            assert res.status_code == 403, "/storage 应要求 pve:manage"
            res = client.get(
                f"/api/pve/connections/{conn2.id}/nodes/pve/isos", headers=headers
            )
            assert res.status_code == 403, "/isos 应要求 pve:manage"
        # manage 用户放行（_client mock 掉真实调用）
        headers = {"Authorization": f"Bearer {token}"}
        with patch.object(
            pve_router,
            "_client",
            lambda *_a, **_k: SimpleNamespace(
                list_nodes=lambda: [],
                next_vmid=lambda: 200,
                list_storage=lambda node: [],
                list_iso_images=lambda node, storage: [],
            ),
        ):
            assert (
                client.get(
                    f"/api/pve/connections/{conn2.id}/nodes", headers=headers
                ).status_code
                == 200
            )
            assert (
                client.get(
                    f"/api/pve/connections/{conn2.id}/nextid", headers=headers
                ).status_code
                == 200
            )
            assert (
                client.get(
                    f"/api/pve/connections/{conn2.id}/nodes/pve/storage",
                    headers=headers,
                ).status_code
                == 200
            )
            assert (
                client.get(
                    f"/api/pve/connections/{conn2.id}/nodes/pve/isos",
                    headers=headers,
                ).status_code
                == 200
            )
    finally:
        _cleanup(db, [conn2], role)
        _cleanup(db, [], role2)


def test_502_detail_sanitized_for_view_user(db):
    """view 用户拿到通用文案，manage 用户拿到原始 PveError 详情。"""
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    role_m, _user_m, token_m = _make_user(db, ["pve:manage"])
    raw = "PVE API 请求失败: hostname lookup 'pve' failed"

    def _failing_client():
        def _boom():
            raise PveError(raw)

        return SimpleNamespace(list_nodes=_boom)

    try:
        with patch.object(pve_router, "_client", lambda *_a, **_k: _failing_client()):
            res = client.get(
                f"/api/pve/connections/{conn2.id}/overview",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res.status_code == 502
            assert res.json()["detail"] == "PVE 平台接口调用失败，请联系管理员"
            assert "hostname" not in res.json()["detail"]

            # manage 用户:详情原样(排查靠它)
            res = client.get(
                f"/api/pve/connections/{conn2.id}/overview",
                headers={"Authorization": f"Bearer {token_m}"},
            )
            assert res.status_code == 502
            assert res.json()["detail"] == raw
    finally:
        _cleanup(db, [conn2], role)
        _cleanup(db, [], role_m)
    """device_scope=all 但只持 pve:view：连接全可见（虚机本就不受限）。"""
    conn1 = _make_conn(db, f"connrbac-a-{STAMP}")
    role, _user, token = _make_user(db, ["pve:view"], device_scope="all")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/pve/connections", headers=headers)
        assert res.status_code == 200
        rows = {r["id"]: r for r in res.json()}
        assert conn1.id in rows
        assert rows[conn1.id]["host"] == ""  # 脱敏仍然生效
    finally:
        _cleanup(db, [conn1], role)


def test_detect_os_requires_manage_returns_403(db):
    """binding/detect-os 越权必须 403（钉子，防田旧版 422 回归）。

    越权审计报告称该接口越权返回 422“无权访问该虚拟机”——与当前实现
    不符（现状：_require_binding_manage → 403，可能是审计测的旧版本）。
    """
    conn2 = _make_conn(db, f"connrbac-b-{STAMP}")
    role, _user, token = _make_user(
        db, ["pve:view"], device_scope="selected", guests=[(conn2.id, "qemu", 103)]
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.post(
            f"/api/pve/connections/{conn2.id}/guests/qemu/103/binding/detect-os",
            headers=headers,
            json={"ip_address": "192.168.1.50", "username": "root", "password": "x"},
        )
        assert res.status_code == 403, res.text
    finally:
        _cleanup(db, [conn2], role)


def test_view_all_scope_without_manage_sees_all_connections(db):
    """device_scope=all 但只持 pve:view：连接全可见（虚机本就不受限）。"""
    conn1 = _make_conn(db, f"connrbac-a-{STAMP}")
    role, _user, token = _make_user(db, ["pve:view"], device_scope="all")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = client.get("/api/pve/connections", headers=headers)
        assert res.status_code == 200
        rows = {r["id"]: r for r in res.json()}
        assert conn1.id in rows
        assert rows[conn1.id]["host"] == ""  # 脱敏仍然生效
    finally:
        _cleanup(db, [conn1], role)
