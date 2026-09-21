"""PVE 异步任务生命周期 API 测试。

create/clone/destroy 都是异步任务(UPID),提交后只有查
``/nodes/{node}/tasks/{upid}/status`` 才知道真实结果。这里钉住:
  * task-status 端点对 running/成功/失败三种任务的归一化口径;
  * upid 校验(防任意字符串注入 PVE 请求路径);
  * 快照名校验(PVE 字符集 + current 保留名)。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database import Base, SessionLocal, engine
from app.models.pve_connection import PveConnection
from app.models.role import Role
from app.models.user import User


class _FakeTaskClient:
    """task_status 返回可配置的固定响应。"""

    def __init__(self, data: dict):
        self._data = data

    def task_status(self, node: str, upid: str):  # noqa: ARG002
        return self._data


@pytest.fixture()
def api():
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.auth import create_access_token

    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"task_role_{stamp}",
        permissions=_json.dumps(["pve:manage", "pve:view"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"tasku_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"task-pve-{stamp}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    # id 存纯量:测试若 rollback/expunge 会 detach 实例,teardown 不碰 ORM 属性。
    conn_id, user_id, role_id = conn.id, user.id, role.id
    token = create_access_token(
        user_id=user_id,
        username=user.username,
        role=user.role,
        permissions=["pve:manage", "pve:view"],
        device_scope="all",
    )
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            headers={"Authorization": f"Bearer {token}"},
            conn_id=conn_id,
        )
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.query(Role).filter(Role.id == role_id).delete()
        db.commit()
        db.close()


_UPID = "UPID:seeed:00349F08:4A48693C:6AAB89D7:qmdel:101:root@pam:"


def _task_status(api, client, node="seeed", upid=_UPID):
    from unittest.mock import patch

    from app.routers import pve as pve_router

    with patch.object(pve_router, "build_client", return_value=client):
        return api.client.get(
            f"/api/pve/connections/{api.conn_id}/task-status",
            headers=api.headers,
            params={"node": node, "upid": upid},
        )


def test_task_status_running_is_not_done(api):
    resp = _task_status(api, _FakeTaskClient({"status": "running"}))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["done"] is False
    assert body["status"] == "running"
    assert body["ok"] is False


def test_task_status_success(api):
    resp = _task_status(api, _FakeTaskClient({"status": "stopped", "exitstatus": "OK"}))
    body = resp.json()
    assert body["done"] is True
    assert body["ok"] is True
    assert body["exitstatus"] == "OK"


def test_task_status_failure_surfaces_exitstatus(api):
    """失败任务把 PVE 的 exitstatus 原样透出——这正是此前静默失败缺失的信息。"""
    resp = _task_status(
        api,
        _FakeTaskClient({"status": "stopped", "exitstatus": "Failed to run vncproxy."}),
    )
    body = resp.json()
    assert body["done"] is True
    assert body["ok"] is False
    assert body["exitstatus"] == "Failed to run vncproxy."


def test_task_status_stops_polling_on_missing_exitstatus(api):
    """终态但 exitstatus 缺失(PVE 不应出现):保守按成功,不展示无意义报错。"""
    resp = _task_status(api, _FakeTaskClient({"status": "stopped"}))
    body = resp.json()
    assert body["done"] is True
    assert body["ok"] is True


def test_task_status_rejects_malformed_upid(api):
    """upid 拼进 PVE 请求路径,非法字符/非 UPID 前缀必须 422 拦下。"""
    for bad in (
        "not-an-upid",
        "UPID:../../etc/passwd",
        "UPID:node:type:1:user:123:%41",
        "UPID:with space:type:1:user:123",
        "",
    ):
        resp = _task_status(api, _FakeTaskClient({}), upid=bad)
        assert resp.status_code == 422, f"upid={bad!r} 应被 422 拒绝"


def test_task_status_rejects_bad_node(api):
    resp = _task_status(api, _FakeTaskClient({}), node="../etc")
    assert resp.status_code == 400
    resp = _task_status(api, _FakeTaskClient({}), node="")
    assert resp.status_code == 400


# ── 快照名(前端占位文案曾用连字符——PVE 本身不接受) ──


@pytest.fixture()
def snap_api():
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.auth import create_access_token

    db = SessionLocal()
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"snap_role_{stamp}",
        permissions=_json.dumps(["pve:manage", "pve:view"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"snapu_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"snap-pve-{stamp}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    # id 存纯量:测试若 rollback/expunge 会 detach 实例,teardown 不碰 ORM 属性。
    conn_id, user_id, role_id = conn.id, user.id, role.id
    token = create_access_token(
        user_id=user_id,
        username=user.username,
        role=user.role,
        permissions=["pve:manage", "pve:view"],
        device_scope="all",
    )
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            headers={"Authorization": f"Bearer {token}"},
            conn_id=conn_id,
        )
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.query(Role).filter(Role.id == role_id).delete()
        db.commit()
        db.close()


class _FakeSnapClient:
    def __init__(self):
        self.created: list[str] = []

    def list_guest_resources(self, force: bool = False):  # noqa: ARG002
        return [{"vmid": 101, "node": "pve1", "type": "qemu", "name": "web-01"}]

    def create_snapshot(self, node, gtype, vmid, snapname, description=""):  # noqa: ARG002
        self.created.append(snapname)
        return "UPID:fake:snapshot:101:root@pam:1663624620:"


def _create_snapshot(api, client, snapname):
    from unittest.mock import patch

    from app.routers import pve as pve_router

    with patch.object(pve_router, "build_client", return_value=client):
        return api.client.post(
            f"/api/pve/connections/{api.conn_id}/guests/qemu/101/snapshots",
            headers=api.headers,
            json={"snapname": snapname},
        )


def test_snapshot_name_rejects_invalid_chars(snap_api):
    """连字符/中文/空格都会被 PVE 拒收,提前 422 并给中文说明。"""
    fake = _FakeSnapClient()
    for bad in ("before-upgrade", "快照", "with space", "a/b"):
        resp = _create_snapshot(snap_api, fake, bad)
        assert resp.status_code == 422, f"snapname={bad!r} 应被 422 拒绝"
        assert "快照名" in resp.json()["detail"]
    # 空串被 pydantic 的 min_length 拦下(响应体是校验错误格式,无 detail 键)
    resp = _create_snapshot(snap_api, fake, "")
    assert resp.status_code == 422
    assert fake.created == []


def test_snapshot_name_rejects_reserved_current(snap_api):
    fake = _FakeSnapClient()
    resp = _create_snapshot(snap_api, fake, "current")
    assert resp.status_code == 422
    assert "保留名" in resp.json()["detail"]
    assert fake.created == []


def test_snapshot_name_accepts_valid(snap_api):
    fake = _FakeSnapClient()
    resp = _create_snapshot(snap_api, fake, "before_upgrade_20260917")
    assert resp.status_code == 200, resp.text
    assert fake.created == ["before_upgrade_20260917"]


# ── LXC 移除钉子(2026-09-17):创建/操作/观测全链路只认 qemu ──


def test_guest_endpoints_reject_lxc(api):
    """LXC 已彻底移除:guest 系列端点对 lxc 一律 400。"""
    cases = (
        ("get", "/guests/lxc/101", None),
        ("get", "/guests/lxc/101/binding", None),
        ("post", "/guests/lxc/101/power", {"action": "start"}),
        ("get", "/guests/lxc/101/snapshots", None),
        ("post", "/guests/lxc/101/clone", {"newid": 999}),
        ("delete", "/guests/lxc/101", None),
    )
    for method, path, body in cases:
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(api.client, method)(
            f"/api/pve/connections/{api.conn_id}{path}",
            headers=api.headers,
            **kwargs,
        )
        assert resp.status_code == 400, (
            f"{method} {path} 应 400,实际 {resp.status_code}"
        )
        assert "LXC" in resp.json()["detail"]


def test_create_guest_rejects_lxc(api):
    resp = api.client.post(
        f"/api/pve/connections/{api.conn_id}/guests/lxc",
        headers=api.headers,
        json={"name": "test-container"},
    )
    assert resp.status_code == 400
    assert "LXC" in resp.json()["detail"]


def test_normalize_guest_skips_lxc():
    """快照归一化直接丢弃 lxc 记录:告警/业务/容器采集等下游一律不感知。"""
    from app.services import pve_guest_status as status_svc

    raw = {
        "vmid": 202,
        "id": "lxc/202",
        "type": "lxc",
        "name": "db",
        "node": "n1",
        "status": "running",
        "cpu": 0.1,
        "mem": 1,
        "maxmem": 2,
        "uptime": 100,
    }
    assert status_svc._normalize_guest(raw) is None
    qemu = dict(raw, id="qemu/101", vmid=101, type="qemu")
    assert status_svc._normalize_guest(qemu) is not None


# ── 新建虚机:QGA 默认开(整条识别/磁盘采集链路的开关) ──


class _FakeCreateClient:
    def __init__(self):
        self.created: list[dict] = []

    def list_nodes(self):
        return [{"node": "pve1"}]

    def list_guest_resources(self, force: bool = False):  # noqa: ARG002
        return []

    def next_vmid(self):
        return 105

    def create_qemu(self, node, params):
        self.created.append({"node": node, **params})
        return "UPID:pve1:qmcreate:105:root@pam:1:"


def _create_guest(api, client, body):
    from unittest.mock import patch

    from app.routers import pve as pve_router

    with patch.object(pve_router, "build_client", return_value=client):
        return api.client.post(
            f"/api/pve/connections/{api.conn_id}/guests/qemu",
            headers=api.headers,
            json=body,
        )


def test_create_guest_enables_qga_agent_by_default(api):
    """不传 agent 时默认 agent=1:IP/OS/文件系统识别与磁盘告警采集都依赖 QGA。"""
    fake = _FakeCreateClient()
    resp = _create_guest(api, fake, {"name": "web-01"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]
    assert resp.json()["node"] == "pve1"
    assert fake.created[0]["agent"] == 1


def test_create_guest_agent_can_be_disabled(api):
    fake = _FakeCreateClient()
    resp = _create_guest(api, fake, {"name": "web-01", "agent": False})
    assert resp.status_code == 200, resp.text
    assert "agent" not in fake.created[0]


# ── VM 配置编辑:cores/memory 走 config,磁盘只扩不缩 ──


class _FakeConfigClient:
    """记录 update/resize 调用;config/status 可注入。"""

    def __init__(self, config: dict | None = None, status: str = "stopped"):
        self.config = (
            config
            if config is not None
            else {
                "cores": 2,
                "memory": 2048,
                "scsi0": "local-lvm:vm-101-disk-0,size=32G",
            }
        )
        self._status = status
        self.updated: list[tuple] = []
        self.resized: list[tuple] = []

    def list_guest_resources(self, force: bool = False):  # noqa: ARG002
        return [{"vmid": 101, "node": "pve1", "type": "qemu", "name": "web-01"}]

    def guest_config(self, node, gtype, vmid):  # noqa: ARG001
        return dict(self.config)

    def guest_status(self, node, gtype, vmid):  # noqa: ARG001
        return {"status": self._status, "vmid": vmid}

    def update_guest_config(self, node, gtype, vmid, params):
        self.updated.append((node, gtype, vmid, params))
        self.config.update(params)
        return None

    def resize_guest_disk(self, node, gtype, vmid, disk, size):
        self.resized.append((node, gtype, vmid, disk, size))
        # 模拟 PVE:按增量累加容量
        import re as _re

        m = _re.search(r"size=(\d+)G", self.config["scsi0"])
        current_g = int(m.group(1)) if m else 0
        delta_g = int(size.strip("+").rstrip("G") or "0")
        self.config["scsi0"] = f"local-lvm:vm-101-disk-0,size={current_g + delta_g}G"
        return None


def _update_config(api, client, body, node=None):
    from unittest.mock import patch

    from app.routers import pve as pve_router

    with patch.object(pve_router, "build_client", return_value=client):
        return api.client.put(
            f"/api/pve/connections/{api.conn_id}/guests/qemu/101/config",
            headers=api.headers,
            params={"node": node} if node else None,
            json=body,
        )


def test_update_config_cores_and_memory(api):
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {"cores": 4, "memory_mb": 4096})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert fake.updated == [("pve1", "qemu", 101, {"cores": 4, "memory": 4096})]
    assert fake.resized == []  # 没动磁盘
    assert body["config"]["cores"] == 4
    assert body["disk_bytes"] == 32 * 1024**3


def test_update_config_resizes_disk_with_delta(api):
    """磁盘按增量(+NG)提交:绝对值在取整误差下可能意外变成缩小请求。"""
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {"disk_gb": 40})
    assert resp.status_code == 200, resp.text
    assert fake.resized == [("pve1", "qemu", 101, "scsi0", "+8G")]
    assert fake.updated == []  # 纯扩盘不动 config
    assert resp.json()["disk_bytes"] == 40 * 1024**3


def test_update_config_rejects_disk_shrink(api):
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {"disk_gb": 32})
    assert resp.status_code == 422
    assert "只允许扩容" in resp.json()["detail"]
    assert fake.resized == []


def test_update_config_rejects_disk_equal(api):
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {"disk_gb": 20})
    assert resp.status_code == 422
    assert fake.resized == []


def test_update_config_rejects_empty_body(api):
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {})
    assert resp.status_code == 422
    assert "至少调整一项" in resp.json()["detail"]
    assert fake.updated == [] and fake.resized == []


def test_update_config_rejects_running_guest(api):
    """运行中的虚机一律 409(与前端按钮口径一致,防 API 直调绕过)。"""
    fake = _FakeConfigClient(status="running")
    resp = _update_config(api, fake, {"cores": 4})
    assert resp.status_code == 409
    assert "请先关机" in resp.json()["detail"]
    assert fake.updated == [] and fake.resized == []


def test_update_config_combined_all_three(api):
    """cores+memory+disk 一次提交:先扩盘再改 config,返回最新状态。"""
    fake = _FakeConfigClient()
    resp = _update_config(api, fake, {"cores": 8, "memory_mb": 8192, "disk_gb": 64})
    assert resp.status_code == 200, resp.text
    assert fake.resized == [("pve1", "qemu", 101, "scsi0", "+32G")]
    assert fake.updated == [("pve1", "qemu", 101, {"cores": 8, "memory": 8192})]
    body = resp.json()
    assert body["disk_bytes"] == 64 * 1024**3
    assert body["config"]["cores"] == 8
