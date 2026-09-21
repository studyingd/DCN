"""Docker 容器解析器 + 容器名/动作校验测试。"""

import json
import threading
import time
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.device_container import DeviceContainer, DeviceDockerStatus
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.routers.containers import _pve_entries
from app.services.containers_collector import ContainerTarget, _persist_batch
from app.services.containers_parser import parse_docker_output
from app.validators import ALLOWED_CONTAINER_ACTIONS, validate_container_name


def _cleanup_pve(db: Session, connection_ids: list[int]) -> None:
    """按外键顺序回收用例创建的 PVE 平台数据（含采集出来的容器快照）。

    这两个用例会往 pve_connections / pve_guest_bindings / device_docker_status /
    device_containers 写行，而 ``_pve_entries(db)`` 读的是全表：不清理的话，
    每跑一次测试库就多一套 "test-pve"/"list-pve"，断言会随运行次数漂移。
    binding id 从库里反查，因此 ``_persist_batch`` 用别的 session 写进来的行
    同样能被回收。
    """
    if not connection_ids:
        return
    binding_ids = [
        row[0]
        for row in db.query(PveGuestBinding.id)
        .filter(PveGuestBinding.connection_id.in_(connection_ids))
        .all()
    ]
    if binding_ids:
        db.query(DeviceContainer).filter(
            DeviceContainer.pve_guest_binding_id.in_(binding_ids)
        ).delete(synchronize_session=False)
        db.query(DeviceDockerStatus).filter(
            DeviceDockerStatus.pve_guest_binding_id.in_(binding_ids)
        ).delete(synchronize_session=False)
        db.query(PveGuestBinding).filter(PveGuestBinding.id.in_(binding_ids)).delete(
            synchronize_session=False
        )
    db.query(PveConnection).filter(PveConnection.id.in_(connection_ids)).delete(
        synchronize_session=False
    )
    db.commit()


LINUX_SAMPLE = """@@ver
24.0.7
@@ps
a1b2c3d4e5f6|nginx|nginx:1.25|Up 3 days|running|0.0.0.0:8080->80/tcp
f6e5d4c3b2a1|redis|redis:7|Exited (0) 2 hours ago|exited|
@@stats
nginx|0.50%|120.5MiB / 16GiB|0.74%
"""


def test_parse_docker_output_full():
    r = parse_docker_output(LINUX_SAMPLE)
    assert r["available"] is True
    assert r["nodocker"] is False
    assert r["version"] == "24.0.7"
    assert len(r["containers"]) == 2

    by_name = {c["name"]: c for c in r["containers"]}
    nginx = by_name["nginx"]
    assert nginx["state"] == "running"
    assert nginx["image"] == "nginx:1.25"
    assert nginx["ports"] == "0.0.0.0:8080->80/tcp"
    assert nginx["cpu_pct"] == 0.5
    assert nginx["mem_pct"] == 0.74
    assert nginx["mem_used_mb"] == 120  # 120.5MiB → 120MB(取整)
    assert nginx["mem_limit_mb"] == 16384  # 16GiB

    redis = by_name["redis"]
    assert redis["state"] == "exited"
    assert redis["cpu_pct"] is None  # 无 stats
    assert redis["ports"] is None


def test_parse_docker_nodocker():
    r = parse_docker_output("@@nodocker")
    assert r["nodocker"] is True
    assert r["available"] is False
    assert r["containers"] == []


def test_parse_docker_empty():
    r = parse_docker_output("")
    assert r["available"] is False
    assert r["containers"] == []


def test_parse_docker_no_stats():
    text = "@@ver\n20.10.0\n@@ps\nabc123|db|postgres:15|Up 1 hour|running|\n"
    r = parse_docker_output(text)
    assert r["available"] is True
    assert r["version"] == "20.10.0"
    assert len(r["containers"]) == 1
    c = r["containers"][0]
    assert c["name"] == "db"
    assert c["cpu_pct"] is None
    assert c["mem_pct"] is None


@pytest.mark.parametrize(
    "name",
    ["nginx", "my_container-1.x", "web01", "A1", "container_with.dots-and_underscores"],
)
def test_container_name_valid(name):
    assert validate_container_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "a b",  # 含空格
        "a;rm -rf /",  # 注入
        "$(reboot)",  # 命令替换
        "a&b",
        "a|b",
        "a`b`",
        "-rm",  # 以 - 开头
        "_x",  # 以 _ 开头(Docker 不允许)
        "x" * 200,  # 超长
    ],
)
def test_container_name_rejected(name):
    with pytest.raises(ValueError):
        validate_container_name(name)


def test_allowed_actions_whitelist():
    assert ALLOWED_CONTAINER_ACTIONS == {"start", "stop", "restart", "remove"}


def test_remove_container_command_assembly():
    """remove 分支的命令拼装:单次 exec 完成 stop+rm(两次独立往返要各握一次
    SSH 手手,慢一倍);`;` 保证 stop 失败不阻塞 rm(容器可能本来就停了)。
    """
    from types import SimpleNamespace

    from app.services.containers_collector import remove_container

    target = SimpleNamespace()
    commands: list[str] = []

    def fake_exec(_target, command):
        commands.append(command)
        return (0, "done", "")

    with patch(
        "app.services.containers_collector._exec_target_command",
        side_effect=fake_exec,
    ):
        code, out, err = remove_container(target, "web", keep_volumes=True)
        assert code == 0 and "已保留" in out
        code, out, err = remove_container(target, "web", keep_volumes=False)
        assert code == 0 and "已删除" in out and "已保留" not in out

    # 单次往返:stop 与 rm(或 rm -v)在同一条命令里
    assert len(commands) == 2
    for cmd in commands:
        assert "docker stop web" in cmd
        assert "; docker rm" in cmd
    assert "rm web" in commands[0] and "-v" not in commands[0]
    assert "rm -v web" in commands[1]


def test_remove_container_stop_failure_not_fatal():
    """stop 报错(容器已停/不存在)但 rm 成功 → 整体成功。

    单命令下用 exit code 模拟:`;` 链里 rm 的退出码就是整条命令的退出码。
    """
    from types import SimpleNamespace

    from app.services.containers_collector import remove_container

    target = SimpleNamespace()

    def fake_exec(_target, command):
        # stop 失败(非零)但 rm 成功(零)——整条命令 exit 0
        assert "; docker rm" in command
        return (0, "", "")

    with patch(
        "app.services.containers_collector._exec_target_command",
        side_effect=fake_exec,
    ):
        code, out, err = remove_container(target, "web", keep_volumes=True)
    assert code == 0
    assert "已删除" in out


def test_remove_container_rm_failure_propagates():
    """rm 失败 → 整体失败,错误信息透传。"""
    from types import SimpleNamespace

    from app.services.containers_collector import remove_container

    target = SimpleNamespace()

    def fake_exec(_target, command):
        assert "; docker rm" in command
        return (1, "", "cannot remove: permission denied")

    with patch(
        "app.services.containers_collector._exec_target_command",
        side_effect=fake_exec,
    ):
        code, out, err = remove_container(target, "web", keep_volumes=True)
    assert code == 1
    assert "permission denied" in err


def test_pve_guest_snapshot_persists_by_binding():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    connection_ids: list[int] = []
    try:
        connection = PveConnection(
            name="test-pve",
            host="127.0.0.1",
            port=8006,
            token_id="root@pam!test",
            token_secret_enc="secret",
            enabled=1,
        )
        db.add(connection)
        db.commit()
        connection_ids.append(connection.id)
        binding = PveGuestBinding(
            connection_id=connection.id,
            guest_type="qemu",
            vmid=101,
            ip_address="192.0.2.101",
            os_system="linux",
            username="root",
            enabled=1,
        )
        db.add(binding)
        db.commit()
        target = ContainerTarget(
            target_id=-connection.id * 1_000_000 - binding.vmid,
            kind="pve_guest",
            entity_id=binding.id,
            name="test-pve/101",
            ip_address=binding.ip_address,
            os_system=binding.os_system,
            username=binding.username,
            password="secret",
            ssh_key=None,
            ssh_port=22,
            winrm_port=5985,
            ssh_host_key=None,
            binding=binding,
        )
        _persist_batch(
            [
                (
                    target,
                    {
                        "available": True,
                        "version": "27.0",
                        "commands": {},
                        "containers": [
                            {
                                "container_id": "abc",
                                "name": "web",
                                "image": "nginx:latest",
                                "state": "running",
                                "status": "Up 1 hour",
                                "ports": None,
                                "cpu_pct": 1.0,
                                "mem_used_mb": 12,
                                "mem_limit_mb": 100,
                                "mem_pct": 12.0,
                            }
                        ],
                    },
                    None,
                )
            ]
        )
        db.rollback()
        status = (
            db.query(DeviceDockerStatus)
            .filter_by(pve_guest_binding_id=binding.id)
            .first()
        )
        row = (
            db.query(DeviceContainer).filter_by(pve_guest_binding_id=binding.id).first()
        )
        assert status is not None and status.container_count == 1
        assert row is not None and row.name == "web" and row.device_id is None
    finally:
        _cleanup_pve(db, connection_ids)
        db.close()


def test_pve_guest_without_containers_is_excluded_from_list():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    connection_ids: list[int] = []
    try:
        connection = PveConnection(
            name="list-pve",
            host="127.0.0.1",
            port=8006,
            token_id="root@pam!list",
            token_secret_enc="secret",
            enabled=1,
        )
        db.add(connection)
        db.commit()
        connection_ids.append(connection.id)
        empty_binding = PveGuestBinding(
            connection_id=connection.id,
            guest_type="qemu",
            vmid=201,
            ip_address="192.0.2.201",
            os_system="linux",
            username="root",
            enabled=1,
        )
        populated_binding = PveGuestBinding(
            connection_id=connection.id,
            guest_type="qemu",
            vmid=202,
            ip_address="192.0.2.202",
            os_system="linux",
            username="root",
            enabled=1,
        )
        db.add_all([empty_binding, populated_binding])
        db.commit()
        db.add_all(
            [
                DeviceDockerStatus(
                    pve_guest_binding_id=empty_binding.id,
                    available=1,
                    container_count=0,
                ),
                DeviceDockerStatus(
                    pve_guest_binding_id=populated_binding.id,
                    available=1,
                    container_count=1,
                ),
            ]
        )
        db.commit()

        entries = _pve_entries(db)
        vmids = {entry["pve_vmid"] for entry in entries}
        assert 201 not in vmids
        assert 202 in vmids
    finally:
        _cleanup_pve(db, connection_ids)
        db.close()


def test_pve_entry_uses_guest_name_from_status_snapshot():
    """PVE 条目的名称/电源状态来自 pve_guest_status 快照(零 PVE API 调用)。

    快照未就绪时回落到绑定身份(QEMU VM {vmid});device_status 同样从快照
    取真实 running/stopped,而不是恒为 unknown。
    """

    from app.services.pve_guest_status import _guests, _lock

    Base.metadata.create_all(engine)
    db = SessionLocal()
    connection_ids: list[int] = []
    try:
        connection = PveConnection(
            name="snap-pve",
            host="127.0.0.1",
            port=8006,
            token_id="root@pam!snap",
            token_secret_enc="secret",
            enabled=1,
        )
        db.add(connection)
        db.commit()
        connection_ids.append(connection.id)
        binding = PveGuestBinding(
            connection_id=connection.id,
            guest_type="qemu",
            vmid=301,
            ip_address="192.0.2.301",
            os_system="linux",
            username="root",
            enabled=1,
        )
        db.add(binding)
        db.commit()
        db.add(
            DeviceDockerStatus(
                pve_guest_binding_id=binding.id, available=1, container_count=1
            )
        )
        db.commit()

        # 快照未就绪:回落到 VM ID 形式
        entries = _pve_entries(db)
        entry = next(e for e in entries if e["pve_vmid"] == 301)
        assert entry["device_name"] == "[snap-pve] QEMU VM 301"
        assert entry["guest_status"] == "unknown"

        # 快照就绪:显示真实名称与状态
        with _lock:
            _guests[(connection.id, 301)] = {
                "guest_type": "qemu",
                "vmid": 301,
                "name": "web-01",
                "status": "running",
            }
        try:
            entries = _pve_entries(db)
            entry = next(e for e in entries if e["pve_vmid"] == 301)
            assert entry["device_name"] == "[snap-pve] web-01"
            assert entry["guest_status"] == "running"
        finally:
            with _lock:
                _guests.pop((connection.id, 301), None)
    finally:
        _cleanup_pve(db, connection_ids)
        db.close()


# ── PVE 虚机容器终端票据(/api/containers/{id}/terminal-ticket + /ws/pve-terminal) ──


def _create_terminal_role_user(db: Session, permissions: list[str]):
    import time

    from app.models.role import Role
    from app.models.user import User
    from app.services.auth import create_access_token

    stamp = time.time_ns()
    role = Role(
        name=f"ctr_term_role_{stamp}",
        permissions=json.dumps(permissions),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"ctr_term_user_{stamp}",
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


def _cleanup_terminal_users(db: Session, role_ids: list[int], user_ids: list[int]):
    from app.models.role import Role
    from app.models.user import User

    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(Role).filter(Role.id.in_(role_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def pve_terminal_env():
    """pve:manage 用户 + 一台 Linux guest binding(带密码凭据)。"""
    import time

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.crypto import encrypt

    Base.metadata.create_all(engine)
    db = SessionLocal()
    connection_ids: list[int] = []
    role_ids: list[int] = []
    user_ids: list[int] = []
    stamp = time.time_ns()
    connection = PveConnection(
        name=f"ctr-term-pve-{stamp}",
        host="127.0.0.1",
        port=8006,
        token_id="root@pam!ctrterm",
        token_secret_enc="secret",
        enabled=1,
    )
    db.add(connection)
    db.commit()
    connection_ids.append(connection.id)
    binding = PveGuestBinding(
        connection_id=connection.id,
        guest_type="qemu",
        vmid=401,
        ip_address="192.0.2.401",
        os_system="linux",
        username="root",
        password_enc=encrypt("guestpass"),
        enabled=1,
    )
    db.add(binding)
    db.commit()
    role, user, token = _create_terminal_role_user(
        db, ["pve:manage", "device:view", "device:remote"]
    )
    role_ids.append(role.id)
    user_ids.append(user.id)
    db.commit()
    client = TestClient(app)
    try:
        yield {
            "db": db,
            "client": client,
            "token": token,
            "user": user,
            "connection": connection,
            "binding": binding,
            "target_id": -(connection.id * 1_000_000 + 401),
        }
    finally:
        _cleanup_terminal_users(db, role_ids, user_ids)
        _cleanup_pve(db, connection_ids)
        db.close()


def test_terminal_ticket_rejects_device_target(pve_terminal_env):
    """正数设备 id 走既有 /api/terminal/ticket,本端点明确拒绝。"""
    resp = pve_terminal_env["client"].post(
        "/api/containers/123/terminal-ticket",
        json={"container": "web"},
        headers={"Authorization": f"Bearer {pve_terminal_env['token']}"},
    )
    assert resp.status_code == 400


def test_terminal_ticket_requires_pve_manage(pve_terminal_env):
    db = pve_terminal_env["db"]
    role, user, token = _create_terminal_role_user(db, ["device:view"])
    db.commit()
    try:
        resp = pve_terminal_env["client"].post(
            f"/api/containers/{pve_terminal_env['target_id']}/terminal-ticket",
            json={"container": "web"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
    finally:
        _cleanup_terminal_users(db, [role.id], [user.id])


def test_terminal_ticket_invalid_container_name(pve_terminal_env):
    resp = pve_terminal_env["client"].post(
        f"/api/containers/{pve_terminal_env['target_id']}/terminal-ticket",
        json={"container": "bad name; rm -rf /"},
        headers={"Authorization": f"Bearer {pve_terminal_env['token']}"},
    )
    assert resp.status_code == 400


def test_terminal_ticket_windows_guest_rejected(pve_terminal_env):
    env = pve_terminal_env
    env["binding"].os_system = "windows"
    env["db"].commit()
    try:
        resp = env["client"].post(
            f"/api/containers/{env['target_id']}/terminal-ticket",
            json={"container": "web"},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
        assert resp.status_code == 400
        assert "RDP" in resp.json()["detail"]
    finally:
        env["binding"].os_system = "linux"
        env["db"].commit()


def test_terminal_ticket_success_payload(pve_terminal_env):
    from app.services.ws_ticket import consume_ticket

    env = pve_terminal_env
    resp = env["client"].post(
        f"/api/containers/{env['target_id']}/terminal-ticket",
        json={"container": "web"},
        headers={"Authorization": f"Bearer {env['token']}"},
    )
    assert resp.status_code == 200
    payload = consume_ticket(resp.json()["ticket"])
    assert payload is not None
    assert payload["kind"] == "pve-remote"
    assert payload["mode"] == "ssh"
    assert payload["binding_id"] == env["binding"].id
    assert payload["conn_id"] == env["connection"].id
    assert payload["gtype"] == "qemu"
    assert payload["vmid"] == 401
    assert payload["container"] == "web"
    assert payload["target_ip"] == "192.0.2.401"
    assert payload["user_id"] == env["user"].id
    # 票据单次有效
    assert consume_ticket(resp.json()["ticket"]) is None


def test_pve_terminal_ws_injects_docker_exec(pve_terminal_env):
    """票据带 container → create_ssh_connection 收到三段式 docker exec 初始命令。

    不真连 SSH:mock pve_console 命名空间里的 create_ssh_connection,
    连上后收 connected 消息即可断言。
    """

    from app.routers import pve_console
    from app.services.ws_ticket import issue_ticket

    env = pve_terminal_env

    class _FakeSession:
        is_connected = True

        async def recv_output(self):
            return None  # reader 立即退出

        async def send_input(self, data):
            pass

        async def resize_pty(self, cols, rows):
            pass

        async def close(self):
            self.is_connected = False

    captured: dict = {}

    async def _fake_create_ssh_connection(device, username, password, **kwargs):
        captured.update(kwargs, host=device.ip_address, username=username)
        return _FakeSession()

    ticket = issue_ticket(
        {
            "kind": "pve-remote",
            "mode": "ssh",
            "binding_id": env["binding"].id,
            "conn_id": env["connection"].id,
            "gtype": "qemu",
            "vmid": 401,
            "target_ip": env["binding"].ip_address,
            "container": "web",
            "user_id": env["user"].id,
            "session_version": int(getattr(env["user"], "session_version", 0)),
        }
    )
    with patch.object(
        pve_console, "create_ssh_connection", _fake_create_ssh_connection
    ):
        with env["client"].websocket_connect(f"/ws/pve-terminal?ticket={ticket}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "connected"
            assert msg["message"] == "已进入容器 web"
        # WS 关闭后 handler 收尾
    cmd = captured.get("initial_command") or ""
    # bash 优先(镜像里没有再退 sh/ash),三段都必须在且顺序固定
    assert cmd.index("docker exec -it web bash") < cmd.index("docker exec -it web sh")
    assert cmd.index("docker exec -it web sh") < cmd.index("docker exec -it web ash")
    assert captured["host"] == "192.0.2.401"
    assert captured["username"] == "root"


def test_pve_terminal_ws_without_container_keeps_plain_shell(pve_terminal_env):
    """无 container 的票据(虚机控制台入口)不受影响:不注入初始命令。"""
    from app.routers import pve_console
    from app.services.ws_ticket import issue_ticket

    env = pve_terminal_env

    class _FakeSession:
        is_connected = True

        async def recv_output(self):
            return None

        async def send_input(self, data):
            pass

        async def resize_pty(self, cols, rows):
            pass

        async def close(self):
            self.is_connected = False

    captured: dict = {}

    async def _fake_create_ssh_connection(device, username, password, **kwargs):
        captured.update(kwargs)
        return _FakeSession()

    ticket = issue_ticket(
        {
            "kind": "pve-remote",
            "mode": "ssh",
            "binding_id": env["binding"].id,
            "conn_id": env["connection"].id,
            "gtype": "qemu",
            "vmid": 401,
            "target_ip": env["binding"].ip_address,
            "user_id": env["user"].id,
            "session_version": int(getattr(env["user"], "session_version", 0)),
        }
    )
    with patch.object(
        pve_console, "create_ssh_connection", _fake_create_ssh_connection
    ):
        with env["client"].websocket_connect(f"/ws/pve-terminal?ticket={ticket}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "connected"
            assert "已进入容器" not in msg["message"]
    assert captured.get("initial_command") is None


def test_pve_terminal_ws_invalid_container_falls_back_to_shell(pve_terminal_env):
    """票据里的容器名非法(绕过端点直接开票)→ 丢弃 container,退回普通 shell。"""
    from app.routers import pve_console
    from app.services.ws_ticket import issue_ticket

    env = pve_terminal_env

    class _FakeSession:
        is_connected = True

        async def recv_output(self):
            return None

        async def send_input(self, data):
            pass

        async def resize_pty(self, cols, rows):
            pass

        async def close(self):
            self.is_connected = False

    captured: dict = {}

    async def _fake_create_ssh_connection(device, username, password, **kwargs):
        captured.update(kwargs)
        return _FakeSession()

    ticket = issue_ticket(
        {
            "kind": "pve-remote",
            "mode": "ssh",
            "binding_id": env["binding"].id,
            "conn_id": env["connection"].id,
            "gtype": "qemu",
            "vmid": 401,
            "target_ip": env["binding"].ip_address,
            "container": "bad name",
            "user_id": env["user"].id,
            "session_version": int(getattr(env["user"], "session_version", 0)),
        }
    )
    with patch.object(
        pve_console, "create_ssh_connection", _fake_create_ssh_connection
    ):
        with env["client"].websocket_connect(f"/ws/pve-terminal?ticket={ticket}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "connected"
            assert "已进入容器" not in msg["message"]
    assert captured.get("initial_command") is None


# ── 采集命令构造:mktemp 防并发互踩 + light 跳过 stats ──


def test_linux_probe_cmd_uses_unique_tempfile():
    """固定 /tmp/dcn_docker_err 会让并发探测互踩同主机同一文件 → mktemp 唯一名。"""
    from app.services.containers_collector import _linux_probe_cmd

    cmd = _linux_probe_cmd()
    assert "/tmp/dcn_docker_err" not in cmd
    assert "mktemp" in cmd
    assert "$DCN_ERR" in cmd
    # 用完即清,不在目标机 /tmp 留残渣
    assert "rm -f $DCN_ERR" in cmd
    # 命令段结构不变(解析器契约)
    for marker in ("@@info", "@@ps", "@@stats", "@@nodocker"):
        assert marker in cmd


def test_linux_probe_cmd_light_removed_regression_guard():
    """light(跳过 stats)模式已回退,勿再加回:持久化是整批 delete+insert,
    跳过 stats 会把整台主机所有容器的指标写成 NULL,直到下个 60s 周期才
    恢复(用户实测:启停一个容器,同机其它容器 CPU/内存全变 "—")。
    stats 实测 ≈2s,10s 只是超时上限——没有理由跳过。
    """
    from app.services.containers_collector import _linux_probe_cmd

    cmd = _linux_probe_cmd()
    assert "docker stats" in cmd and "@@stats" in cmd


def test_spawn_collect_one_dedupes_same_target():
    """同目标在采时不再起新线程(防连点操作并发探测同一台主机)。"""
    from app.services import containers_collector as cc

    started: list[int] = []

    def fake_collect_one(target_id, *, light=False):
        started.append(target_id)

    release = threading.Event()

    def slow_collect_one(target_id, *, light=False):
        started.append(target_id)
        release.wait(timeout=2)

    with patch.object(cc, "_collect_one", side_effect=slow_collect_one):
        cc._spawn_collect_one(7)  # 占住目标 7
        with patch.object(cc, "_collect_one", side_effect=fake_collect_one):
            cc._spawn_collect_one(7)  # 应被去重闸门挡掉
        release.set()
        # 等占位线程退出,清掉 inflight
        for _ in range(50):
            with cc._collect_one_lock:
                if 7 not in cc._collect_one_inflight:
                    break
            time.sleep(0.02)
    assert started == [7]


# ── 控制端点权限分域:pve:manage 单独持有人不得控制物理设备容器 ──


def test_control_rejects_pve_manage_only_user_on_device():
    """仅持 pve:manage 的用户(device_scope=all)不能启停物理设备容器。"""
    import time as _time

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.device import Device
    from app.models.rack import Rack
    from app.models.role import Role
    from app.models.room import Room
    from app.models.user import User
    from app.services.auth import create_access_token
    from app.services.device_credentials import set_credentials

    Base.metadata.create_all(engine)
    db = SessionLocal()
    stamp = _time.time_ns()
    role = Role(
        name=f"pveonly_role_{stamp}",
        permissions=json.dumps(["pve:manage"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"pveonly_user_{stamp}",
        password="irrelevant",
        role="viewer",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    room = Room(name=f"ctr-room-{stamp}", location="L1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"ctr-rack-{stamp}", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"ctr-dev-{stamp}",
        type="server",
        ip_address=f"10.77.{stamp % 200}.{stamp % 250}",
        os_system="Rocky Linux 9.4",
        status="online",
    )
    db.add(device)
    db.flush()
    set_credentials(device, "root", "devpass", None)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["pve:manage"],
        device_scope="all",
    )
    client = TestClient(app)
    try:
        resp = client.post(
            f"/api/containers/{device.id}/control",
            json={"action": "stop", "name": "web"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "无权" in resp.json()["detail"]
    finally:
        db.rollback()
        db.query(Device).filter(Device.id == device.id).delete(
            synchronize_session=False
        )
        db.query(Rack).filter(Rack.id == rack.id).delete(synchronize_session=False)
        db.query(Room).filter(Room.id == room.id).delete(synchronize_session=False)
        db.query(User).filter(User.id == user.id).delete(synchronize_session=False)
        db.query(Role).filter(Role.id == role.id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_probe_commands_pass_shell_syntax_check():
    """探测命令必须是合法 shell 语法。

    回归钉:mktemp 改造时漏了赋值后的 `;`,`DCN_ERR=$(...) if ...` 被远程
    bash 拒收(exit 1 语法错误),一轮周期就把所有目标写成 available=0、
    容器行清零,容器管理页整页空白。用 sh -n 静态语法检查钉住。
    """
    import subprocess

    from app.services.containers_collector import _linux_probe_cmd

    cmd = _linux_probe_cmd()
    r = subprocess.run(["sh", "-n", "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 命令必须是完整语句:赋值后要有分隔符,不能出现 `$(...) if` / `fi rm`
    assert "$(mktemp /tmp/dcn-docker-err.XXXXXX);" in cmd
    assert "fi; rm -f $DCN_ERR" in cmd


def test_windows_probe_ps_matches_legacy_contract():
    """Windows 全量命令必须与 mktemp 改造前的字符串逐字一致(PowerShell 语义未动)。"""
    from app.services.containers_collector import _windows_probe_ps

    expected = (
        "if (Get-Command docker -ErrorAction SilentlyContinue) { "
        "Write-Output '@@info'; try { $v = docker version --format '{{.Server.Version}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message }; "
        "Write-Output '@@ps'; try { $v = docker ps -a --format "
        "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}|{{.Ports}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message }; "
        "Write-Output '@@stats'; try { $v = docker stats --no-stream --format "
        "'{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message } "
        "} else { Write-Output '@@nodocker' }"
    )
    assert _windows_probe_ps() == expected
