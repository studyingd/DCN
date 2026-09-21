"""删除虚拟机的清理范围测试。

两件事必须成立:
  * ``parse_guest_volumes`` 能区分"会随虚机一起销毁的磁盘卷"和"PVE 不会碰的
    ISO 介质 / LXC 宿主机绑定挂载"，否则回执会把保留项说成已清理;
  * 虚机在 PVE 侧销毁后，平台侧引用(运维接入、容器快照、业务关联)一并清掉，
    否则容器采集和业务监控会继续去连一台不存在的虚机，产生"永远离线"的假告警。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database import Base, SessionLocal, engine
from app.models.business import Business, BusinessPveGuest
from app.models.device_container import (
    ContainerAction,
    DeviceContainer,
    DeviceDockerStatus,
)
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.routers.pve import _purge_guest_local_records
from app.services.pve import parse_disk_size, parse_guest_volumes

# ── 卷解析(纯函数) ──


def test_parse_disk_size_handles_units_and_raw_bytes():
    assert parse_disk_size("32G") == 32 * 1024**3
    assert parse_disk_size("512M") == 512 * 1024**2
    assert parse_disk_size("32212254720") == 32212254720
    assert parse_disk_size("") is None
    assert parse_disk_size("garbage") is None


def test_parse_guest_volumes_classifies_qemu_disks():
    """真实 PVE 配置形态:磁盘卷、unused 残留卷、CD-ROM 各归各类。"""
    config = {
        "scsi0": "local-lvm:vm-101-disk-0,iothread=1,size=100G",
        "unused0": "local-lvm:vm-101-disk-1,size=8G",
        "ide2": "local:iso/CentOS_7.iso,media=cdrom,size=4494M",
        "name": "web-01",
        "cores": 4,
        "scsihw": "virtio-scsi-single",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
    }
    volumes = parse_guest_volumes(config)

    by_key = {vol["key"]: vol for vol in volumes}
    assert set(by_key) == {"scsi0", "unused0", "ide2"}
    assert by_key["scsi0"]["kind"] == "disk"
    assert by_key["scsi0"]["volid"] == "local-lvm:vm-101-disk-0"
    assert by_key["scsi0"]["size_bytes"] == 100 * 1024**3
    # 已从配置卸载、却仍占着存储的残留卷必须单独标出，需要显式参数才会清掉。
    assert by_key["unused0"]["kind"] == "unused"
    # ISO 是共享安装介质，PVE 销毁虚机时不会删，不能被算进"已清理"。
    assert by_key["ide2"]["kind"] == "cdrom"
    # 非磁盘键不进清单
    assert "name" not in by_key and "net0" not in by_key


def test_parse_guest_volumes_orders_destroyable_first():
    config = {
        "ide2": "local:iso/x.iso,media=cdrom,size=1G",
        "unused0": "local-lvm:vm-1-disk-1,size=1G",
        "scsi0": "local-lvm:vm-1-disk-0,size=1G",
    }
    kinds = [vol["kind"] for vol in parse_guest_volumes(config)]
    assert kinds == ["disk", "unused", "cdrom"]


# ── 平台侧引用清理 ──


@pytest.fixture()
def guest_env():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    stamp = datetime.now(timezone.utc).timestamp()
    vmid = 101

    conn = PveConnection(
        name=f"del-pve-{stamp}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=vmid,
        ip_address="10.8.0.101",
        os_system="linux",
    )
    db.add(binding)
    db.flush()
    container = DeviceContainer(
        pve_guest_binding_id=binding.id,
        container_id="abc123",
        name="web",
        state="running",
    )
    docker_status = DeviceDockerStatus(pve_guest_binding_id=binding.id, available=1)
    audit = ContainerAction(
        pve_guest_binding_id=binding.id,
        container_name="web",
        action="restart",
        success=1,
    )
    business = Business(name=f"del-biz-{stamp}")
    db.add_all([container, docker_status, audit, business])
    db.flush()
    link = BusinessPveGuest(
        business_id=business.id,
        connection_id=conn.id,
        guest_type="qemu",
        vmid=vmid,
        guest_name="web-01",
    )
    # 别的虚机的关联不能被误删
    other_link = BusinessPveGuest(
        business_id=business.id,
        connection_id=conn.id,
        guest_type="qemu",
        vmid=999,
        guest_name="other",
    )
    db.add_all([link, other_link])
    db.commit()

    ids = {
        "conn": conn.id,
        "binding": binding.id,
        "container": container.id,
        "docker": docker_status.id,
        "audit": audit.id,
        "business": business.id,
        "link": link.id,
        "other_link": other_link.id,
    }
    try:
        yield SimpleNamespace(db=db, vmid=vmid, **ids)
    finally:
        db.query(ContainerAction).filter(ContainerAction.id == ids["audit"]).delete()
        db.query(BusinessPveGuest).filter(
            BusinessPveGuest.business_id == ids["business"]
        ).delete(synchronize_session=False)
        db.query(Business).filter(Business.id == ids["business"]).delete()
        db.query(DeviceContainer).filter(DeviceContainer.id == ids["container"]).delete(
            synchronize_session=False
        )
        db.query(DeviceDockerStatus).filter(
            DeviceDockerStatus.id == ids["docker"]
        ).delete(synchronize_session=False)
        db.query(PveGuestBinding).filter(PveGuestBinding.id == ids["binding"]).delete(
            synchronize_session=False
        )
        db.query(PveConnection).filter(PveConnection.id == ids["conn"]).delete()
        db.commit()
        db.close()


def test_purge_guest_local_records_removes_platform_references(guest_env):
    env = guest_env
    removed = _purge_guest_local_records(env.db, env.conn, "qemu", env.vmid)

    assert removed == {
        "bindings": 1,
        "containers": 1,
        "docker_status": 1,
        "business_links": 1,
    }
    assert env.db.get(PveGuestBinding, env.binding) is None
    assert env.db.get(DeviceContainer, env.container) is None
    assert env.db.get(DeviceDockerStatus, env.docker) is None
    assert env.db.get(BusinessPveGuest, env.link) is None
    # 同业务下别的虚机关联、以及审计流水必须保留。
    assert env.db.get(BusinessPveGuest, env.other_link) is not None
    assert env.db.get(ContainerAction, env.audit) is not None


def test_purge_guest_local_records_is_idempotent(guest_env):
    """重复清理(或清理一台从未接入平台的虚机)不应报错。"""
    env = guest_env
    _purge_guest_local_records(env.db, env.conn, "qemu", env.vmid)
    again = _purge_guest_local_records(env.db, env.conn, "qemu", env.vmid)
    assert again == {
        "bindings": 0,
        "containers": 0,
        "docker_status": 0,
        "business_links": 0,
    }


# ── 删除端点行为 ──


# 显式传 None 表示 wait_task_done 超时(任务仍在进行);需与"未传参"区分。
_TASK_UNSET = object()


class _FakeDeleteClient:
    """只实现删除链路用到的方法，并记录调用顺序与参数。

    ``task_result`` 控制 destroy 任务的终态:
      * dict(status="stopped", exitstatus="OK") → 任务成功;
      * dict(status="stopped", exitstatus="ERROR: ...") → 任务失败;
      * None(显式传入) → wait_task_done 超时(任务仍在进行);
      * 未传 → 默认任务成功。
    """

    def __init__(
        self,
        status: str = "stopped",
        config: dict | None = None,
        task_result: dict | None = _TASK_UNSET,
        guests: list[dict] | None = None,
    ):
        self.status = status
        self.config = (
            config
            if config is not None
            else {
                "scsi0": "local-lvm:vm-101-disk-0,size=100G",
                "unused0": "local-lvm:vm-101-disk-1,size=8G",
                "ide2": "local:iso/CentOS_7.iso,media=cdrom,size=4494M",
            }
        )
        self.task_result = (
            {"status": "stopped", "exitstatus": "OK"}
            if task_result is _TASK_UNSET
            else task_result
        )
        self.calls: list[tuple] = []
        self.stopped_after_power = False
        self._guests = (
            guests
            if guests is not None
            else [{"vmid": 101, "node": "pve1", "type": "qemu", "name": "web-01"}]
        )

    def list_guest_resources(self, force: bool = False):  # noqa: ARG002
        return self._guests

    def list_nodes(self):
        return [{"node": "pve1"}]

    def guest_config(self, node, gtype, vmid):
        self.calls.append(("config", node, gtype, vmid))
        return self.config

    def guest_status(self, node, gtype, vmid):
        self.calls.append(("status", node, gtype, vmid))
        return {"status": self.status, "vmid": vmid}

    def power(self, node, gtype, vmid, action):
        self.calls.append(("power", action, node, gtype, vmid))
        if action == "stop":
            self.status = "stopped"
            self.stopped_after_power = True
        return "UPID:fake"

    def wait_guest_stopped(self, node, gtype, vmid, timeout=60, interval=2.0):  # noqa: ARG002
        self.calls.append(("wait_stopped", timeout))
        return self.status == "stopped"

    def delete_guest(
        self, node, gtype, vmid, purge=True, destroy_unreferenced_disks=True
    ):
        self.calls.append(
            ("delete", node, gtype, vmid, purge, destroy_unreferenced_disks)
        )
        return "UPID:fake:qmdel:101:root@pam:1663624620:"

    def task_status(self, node, upid):
        # 仅当任务已到终态时返回结果;未完成时返回 running
        if self.task_result is None:
            return {"status": "running"}
        return self.task_result

    def wait_task_done(self, node, upid, timeout=60, interval=2.0):  # noqa: ARG002
        self.calls.append(("wait_task", timeout))
        return self.task_result


@pytest.fixture()
def delete_api():
    """返回 (headers, conn_id, db, 清理函数)；用 pve:manage 权限的管理员。"""
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.role import Role
    from app.models.user import User
    from app.services.auth import create_access_token

    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"pve_del_role_{stamp}",
        permissions=_json.dumps(["pve:manage", "pve:view"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"pvedel_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"del-api-{stamp}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["pve:manage", "pve:view"],
        device_scope="all",
    )
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            headers={"Authorization": f"Bearer {token}"},
            conn_id=conn.id,
            db=db,
        )
    finally:
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


def test_delete_running_guest_is_rejected_without_force(delete_api):
    """PVE 不允许销毁运行中的虚机；必须给出明确原因而不是丢一个 502。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="running")
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_api.client.delete(
            f"/api/pve/connections/{delete_api.conn_id}/guests/qemu/101",
            headers=delete_api.headers,
        )

    assert resp.status_code == 409
    assert "正在运行" in resp.json()["detail"]
    assert not any(call[0] == "delete" for call in fake.calls)


def test_delete_force_stops_then_destroys_with_cleanup_params(delete_api):
    """强制删除先停机，再带 purge + destroy-unreferenced-disks 销毁。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="running")
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_api.client.delete(
            f"/api/pve/connections/{delete_api.conn_id}/guests/qemu/101",
            headers=delete_api.headers,
            params={"purge": True, "destroy_unreferenced_disks": True, "force": True},
        )

    assert resp.status_code == 200, resp.text
    order = [call[0] for call in fake.calls]
    assert order.index("power") < order.index("delete")
    delete_call = next(call for call in fake.calls if call[0] == "delete")
    # 关键:两个参数都要真的传给 PVE，否则存储上会留下孤儿磁盘。
    assert delete_call[4] is True and delete_call[5] is True
    body = resp.json()
    assert body["stopped_first"] is True
    assert body["success"] is True
    kinds = {vol["key"]: vol["kind"] for vol in body["volumes"]}
    assert kinds == {"scsi0": "disk", "unused0": "unused", "ide2": "cdrom"}


def test_delete_report_distinguishes_destroyed_from_preserved(delete_api):
    """回执要如实区分：磁盘/残留卷被清理，ISO 按 PVE 规则保留。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="stopped")
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_api.client.delete(
            f"/api/pve/connections/{delete_api.conn_id}/guests/qemu/101",
            headers=delete_api.headers,
        )

    assert resp.status_code == 200, resp.text
    message = resp.json()["message"]
    assert "清理 1 个磁盘卷" in message
    assert "清理 1 个 unused 残留卷" in message
    assert "ISO/绑定挂载按 PVE 规则保留" in message


def test_delete_without_purge_keeps_volumes_and_says_so(delete_api):
    """关掉 purge 时卷会留在存储上，回执必须明说，不能让用户以为已经清干净。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="stopped")
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_api.client.delete(
            f"/api/pve/connections/{delete_api.conn_id}/guests/qemu/101",
            headers=delete_api.headers,
            params={"purge": False},
        )

    assert resp.status_code == 200, resp.text
    delete_call = next(call for call in fake.calls if call[0] == "delete")
    assert delete_call[4] is False
    assert "仍保留在存储上" in resp.json()["message"]


# ── 销毁任务生命周期:等 PVE 任务终态再清平台记录 ──


@pytest.fixture()
def delete_env():
    """delete_api + 该虚机的平台侧记录(绑定),验证保留/清理语义。"""
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.role import Role
    from app.models.user import User
    from app.services.auth import create_access_token

    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"pve_del2_role_{stamp}",
        permissions=_json.dumps(["pve:manage", "pve:view"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"pvedel2_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    conn = PveConnection(
        name=f"del2-api-{stamp}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.8.0.101",
        os_system="linux",
    )
    db.add(binding)
    db.commit()
    # rollback 会 expire 实例属性、测试断言还会 expunge_all;所有 id 先存成纯量,
    # teardown 不再碰 ORM 实例(API 已删行时 refresh 会抛 ObjectDeletedError)。
    binding_id, conn_id, user_id, role_id = binding.id, conn.id, user.id, role.id
    token = create_access_token(
        user_id=user.id,
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
            binding_id=binding_id,
            db=db,
        )
    finally:
        # 先 API 后 db 断言的口径:HTTP 提交对持有事务的本会话不可见,rollback
        # 刷新快照;expunge_all 清 identity map,避免过期实例 refresh 报错。
        db.rollback()
        db.expunge_all()
        db.query(PveGuestBinding).filter(PveGuestBinding.id == binding_id).delete(
            synchronize_session=False
        )
        db.query(PveConnection).filter(PveConnection.id == conn_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.query(Role).filter(Role.id == role_id).delete()
        db.commit()
        db.close()


def test_delete_task_failure_keeps_local_records(delete_env):
    """PVE 销毁任务失败(磁盘锁/HA 引用)时,虚机还在,平台侧记录必须保留。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(
        status="stopped",
        task_result={"status": "stopped", "exitstatus": "ERROR: volume removal failed"},
    )
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_env.client.delete(
            f"/api/pve/connections/{delete_env.conn_id}/guests/qemu/101",
            headers=delete_env.headers,
        )

    assert resp.status_code == 502
    assert "ERROR: volume removal failed" in resp.json()["detail"]
    assert "记录已保留" in resp.json()["detail"]
    # 任务真的发出去了,但本地绑定还在
    assert any(call[0] == "delete" for call in fake.calls)
    delete_env.db.rollback()
    delete_env.db.expunge_all()
    assert delete_env.db.get(PveGuestBinding, delete_env.binding_id) is not None


def test_delete_task_timeout_keeps_local_records(delete_env):
    """销毁任务超时未完成:不清本地,提示重试(重试走"已不存在"分支收敛)。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="stopped", task_result=None)
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_env.client.delete(
            f"/api/pve/connections/{delete_env.conn_id}/guests/qemu/101",
            headers=delete_env.headers,
        )

    assert resp.status_code == 502
    assert "仍在进行" in resp.json()["detail"]
    assert "重试删除" in resp.json()["detail"]
    delete_env.db.rollback()
    delete_env.db.expunge_all()
    assert delete_env.db.get(PveGuestBinding, delete_env.binding_id) is not None


def test_delete_when_guest_already_gone_purges_local(delete_env):
    """虚机已从 PVE 消失(直接在 PVE 界面删过/销毁任务已成功):重试删除
    直接清理平台引用,不报错——修复此前"记录永久残留→永远离线假告警"的坑。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="stopped", guests=[])
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_env.client.delete(
            f"/api/pve/connections/{delete_env.conn_id}/guests/qemu/101",
            headers=delete_env.headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert "PVE 侧已不存在" in body["message"]
    assert body["purged_local"]["bindings"] == 1
    # 销毁流程都没走(虚机已不在,无需 config/destroy)
    assert not any(call[0] in ("config", "delete") for call in fake.calls)
    delete_env.db.rollback()
    delete_env.db.expunge_all()
    assert delete_env.db.get(PveGuestBinding, delete_env.binding_id) is None


def test_delete_task_success_purges_local_records(delete_env):
    """任务成功(OK)后照旧清理平台引用——既有行为不变。"""
    from unittest.mock import patch

    from app.routers import pve as pve_router

    fake = _FakeDeleteClient(status="stopped")
    with patch.object(pve_router, "build_client", return_value=fake):
        resp = delete_env.client.delete(
            f"/api/pve/connections/{delete_env.conn_id}/guests/qemu/101",
            headers=delete_env.headers,
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["purged_local"]["bindings"] == 1
    assert any(call[0] == "wait_task" for call in fake.calls)
    delete_env.db.rollback()
    delete_env.db.expunge_all()
    assert delete_env.db.get(PveGuestBinding, delete_env.binding_id) is None
