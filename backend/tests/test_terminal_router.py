"""terminal 路由测试：票据签发端点 + WebSocket 入口校验 + RDP 指令净化等纯函数。

不真连 SSH/guacd：WebSocket 只覆盖“票据/凭据/权限/设备”这些入口校验分支，
真正的交互式桥接循环需要活体连接，不在单测范围内。
DB 行（角色/用户/设备）自建自清；内存票据/凭据在用例内消费掉，无残留。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.routers import terminal as terminal_router
from app.routers.terminal import (
    TerminalTicketRequest,
    _access_jti,
    _cleanup_drive_dir,
    _first_mouse_coords,
    _needs_rdp_rewrite,
    _resolve_credentials,
    _sanitize_rdp_input,
    _sanitize_rdp_instructions,
)
from app.services.auth import create_access_token, decode_token
from app.services.crypto import decrypt, encrypt
from app.services.guacamole import _build_instruction
from app.services.ws_ticket import (
    consume_credential,
    consume_ticket,
    issue_credential,
    issue_ticket,
)

client = TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── TerminalTicketRequest 校验 ──


def test_ticket_request_conn_type_whitelist():
    assert TerminalTicketRequest(device_id=1, conn_type="ssh").conn_type == "ssh"
    assert TerminalTicketRequest(device_id=1, conn_type="rdp").conn_type == "rdp"
    with pytest.raises(ValidationError):
        TerminalTicketRequest(device_id=1, conn_type="telnet")


def test_ticket_request_field_bounds():
    with pytest.raises(ValidationError):
        TerminalTicketRequest(device_id=0)  # ge=1
    with pytest.raises(ValidationError):
        TerminalTicketRequest(device_id=1, width=100)  # < 320
    with pytest.raises(ValidationError):
        TerminalTicketRequest(device_id=1, height=99999)  # > 4320


# ── _resolve_credentials ──


class _StubDevice:
    def __init__(self, remote_username=None, password=None):
        self.remote_username = remote_username
        self.remote_password_enc = encrypt(password) if password else None


def test_resolve_credentials_falls_back_to_device_secret():
    device = _StubDevice(remote_username="devuser", password="devpass")
    # 空串/None 密码都表示“用设备保存的凭据”
    assert _resolve_credentials(device, "", None) == ("devuser", "devpass")
    assert _resolve_credentials(device, "", "") == ("devuser", "devpass")


def test_resolve_credentials_request_overrides():
    device = _StubDevice(remote_username="devuser", password="devpass")
    assert _resolve_credentials(device, "req", "reqpass") == ("req", "reqpass")
    # 只覆盖用户名，密码仍回退设备值
    assert _resolve_credentials(device, "u", "") == ("u", "devpass")


def test_resolve_credentials_empty_device():
    device = _StubDevice()
    assert _resolve_credentials(device, "", "") == ("", "")


# ── _access_jti ──


def test_access_jti_from_cookie():
    token = create_access_token(user_id=1, username="a", role="r")
    expected = decode_token(token)["jti"]
    req = SimpleNamespace(cookies={"dcn_access": token}, headers={})
    assert _access_jti(req) == expected


def test_access_jti_from_bearer_header():
    token = create_access_token(user_id=2, username="b", role="r")
    expected = decode_token(token)["jti"]
    req = SimpleNamespace(cookies={}, headers={"Authorization": f"Bearer {token}"})
    assert _access_jti(req) == expected


def test_access_jti_missing_or_invalid():
    assert _access_jti(SimpleNamespace(cookies={}, headers={})) == ""
    bad = SimpleNamespace(cookies={"dcn_access": "not-a-jwt"}, headers={})
    assert _access_jti(bad) == ""


# ── _cleanup_drive_dir ──


def test_cleanup_drive_dir_removes_sanitized_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_router, "GUACD_DRIVE_ROOT", str(tmp_path))
    # 合法 id：目录被删除，根目录保留
    safe_dir = tmp_path / "abc-123_XY"
    safe_dir.mkdir()
    _cleanup_drive_dir("abc-123_XY")
    assert not safe_dir.exists()
    assert tmp_path.exists()


def test_cleanup_drive_dir_strips_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_router, "GUACD_DRIVE_ROOT", str(tmp_path))
    # "../../etc" 经净化只剩字母 → 只可能删到 root/etc，绝不会越界
    (tmp_path / "etc").mkdir()
    _cleanup_drive_dir("../../etc")
    assert not (tmp_path / "etc").exists()
    assert tmp_path.exists()


def test_cleanup_drive_dir_empty_id_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_router, "GUACD_DRIVE_ROOT", str(tmp_path))
    # 净化后为空 → 直接返回，不抛异常、不动根目录
    _cleanup_drive_dir("///")
    assert tmp_path.exists()


# ── RDP 指令净化 ──


def test_sanitize_instructions_size_updates_dimensions():
    rebuilt, w, h, clamped = _sanitize_rdp_instructions(
        [("size", ["800", "600"])], 1024, 768
    )
    assert (w, h) == (800, 600)
    assert clamped == 0
    assert rebuilt == [_build_instruction("size", "800", "600")]


def test_sanitize_instructions_clamps_mouse_to_edges():
    # limit=200 → 边缘内缩 2，最大 197；500 被夹到 197
    rebuilt, w, h, clamped = _sanitize_rdp_instructions(
        [("size", ["200", "200"]), ("mouse", ["500", "500"])], 100, 100
    )
    assert (w, h) == (200, 200)
    assert clamped == 1
    assert rebuilt[-1] == _build_instruction("mouse", "197", "197")


def test_sanitize_instructions_drop_server_mouse():
    rebuilt, w, h, clamped = _sanitize_rdp_instructions(
        [("mouse", ["50", "50"]), ("blob", ["1", "data"])],
        100,
        100,
        drop_server_mouse=True,
    )
    # 服务端鼠标更新被丢弃，其它指令保留
    assert rebuilt == [_build_instruction("blob", "1", "data")]
    assert clamped == 0


def test_sanitize_instructions_invalid_size_ignored():
    rebuilt, w, h, clamped = _sanitize_rdp_instructions(
        [("size", ["abc", "def"])], 100, 100
    )
    assert (w, h) == (100, 100)  # 解析失败保持原尺寸
    assert rebuilt == [_build_instruction("size", "abc", "def")]


def test_sanitize_input_clamps_mouse_bytes():
    raw = _build_instruction("mouse", "500", "500")
    out, w, h, clamped = _sanitize_rdp_input(raw, 100, 100)
    assert out == _build_instruction("mouse", "97", "97")
    assert (w, h, clamped) == (100, 100, 1)


def test_sanitize_input_garbage_passthrough():
    out, w, h, clamped = _sanitize_rdp_input(b"garbage", 100, 100)
    assert out == b"garbage"
    assert (w, h, clamped) == (100, 100, 0)


# ── 端点/WebSocket 集成脚手架 ──


def _create_role_user(db: Session, permissions: list[str], scope: str = "all"):
    import time

    stamp = time.time_ns()
    role = Role(
        name=f"term_role_{stamp}",
        permissions=json.dumps(permissions),
        device_scope=scope,
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"termuser_{stamp}",
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
        device_scope=scope,
    )
    return role, user, token


def _cleanup(db, user_ids, role_ids, device_ids, rack_ids, room_ids):
    db.query(Device).filter(Device.id.in_(device_ids)).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.query(Role).filter(Role.id.in_(role_ids)).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.id.in_(rack_ids)).delete(synchronize_session=False)
    db.query(Room).filter(Room.id.in_(room_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def tenv():
    """device:remote 用户 + 一台 Linux 设备 + 一台 Windows 设备。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    role, user, token = _create_role_user(db, ["device:remote", "device:view"])
    room = Room(name="Terminal Room", location="L1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="Rack-T", type="cabinet")
    db.add(rack)
    db.flush()
    linux = Device(
        rack_id=rack.id,
        name="term-linux",
        type="server",
        ip_address="10.3.0.1",
        os_system="Rocky Linux 9.4",
        status="online",
    )
    windows = Device(
        rack_id=rack.id,
        name="term-win",
        type="server",
        ip_address="10.3.0.2",
        os_system="Microsoft Windows Server 2019",
        status="online",
    )
    db.add_all([linux, windows])
    db.commit()
    from app.services.device_credentials import set_credentials

    set_credentials(linux, "root", "linuxpass", None)
    db.commit()
    try:
        yield SimpleNamespace(
            db=db,
            token=token,
            role=role,
            user=user,
            room=room,
            rack=rack,
            linux=linux,
            windows=windows,
        )
    finally:
        _cleanup(db, [user.id], [role.id], [linux.id, windows.id], [rack.id], [room.id])
        db.close()


# ── create_terminal_ticket 端点 ──


def test_create_ticket_device_not_found(tenv):
    resp = client.post(
        "/api/terminal/ticket",
        json={"device_id": 999999999, "conn_type": "ssh"},
        headers=_auth(tenv.token),
    )
    assert resp.status_code == 404


def test_create_ticket_ssh_to_windows_rejected(tenv):
    resp = client.post(
        "/api/terminal/ticket",
        json={"device_id": tenv.windows.id, "conn_type": "ssh"},
        headers=_auth(tenv.token),
    )
    assert resp.status_code == 400
    assert "RDP" in resp.json()["detail"]


def test_create_ticket_container_with_rdp_rejected(tenv):
    resp = client.post(
        "/api/terminal/ticket",
        json={"device_id": tenv.linux.id, "conn_type": "rdp", "container": "web"},
        headers=_auth(tenv.token),
    )
    assert resp.status_code == 400


def test_create_ticket_invalid_container_name_rejected(tenv):
    resp = client.post(
        "/api/terminal/ticket",
        json={"device_id": tenv.linux.id, "conn_type": "ssh", "container": "bad name"},
        headers=_auth(tenv.token),
    )
    assert resp.status_code == 400


def test_create_ticket_requires_permission():
    db = SessionLocal()
    role, user, token = _create_role_user(db, ["device:view"])  # 缺 device:remote
    db.commit()
    try:
        resp = client.post(
            "/api/terminal/ticket",
            json={"device_id": 1, "conn_type": "ssh"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _cleanup(db, [user.id], [role.id], [], [], [])
        db.close()


def test_create_ticket_success_resolves_device_credentials(tenv):
    resp = client.post(
        "/api/terminal/ticket",
        json={"device_id": tenv.linux.id, "conn_type": "ssh"},
        headers=_auth(tenv.token),
    )
    assert resp.status_code == 200
    ticket = resp.json()["ticket"]
    payload = consume_ticket(ticket)
    assert payload is not None
    assert payload["device_id"] == tenv.linux.id
    assert payload["conn_type"] == "ssh"
    # 票据本身不含明文密码，只带一个一次性凭据引用
    assert "password" not in payload
    creds = consume_credential(payload["credential_ref"])
    assert creds is not None
    assert creds["username"] == "root"
    assert creds["password"] == "linuxpass"
    # 票据/凭据均单次消费，再用返回 None
    assert consume_ticket(ticket) is None
    assert consume_credential(payload["credential_ref"]) is None


# ── terminal_ws WebSocket 入口校验 ──


def test_ws_invalid_ticket_closes_4001():
    with client.websocket_connect("/ws/terminal/1?ticket=bogus") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4001


def test_ws_expired_credential_closes_4001(tenv):
    # 合法票据 + 合法用户,但 credential_ref 指向不存在的凭据 → 凭据已过期分支
    ticket = issue_ticket(
        {
            "device_id": tenv.linux.id,
            "conn_type": "ssh",
            "credential_ref": "nonexistent-ref",
            "user_id": tenv.user.id,
            "system_username": tenv.user.username,
            "access_jti": "",
            "session_version": int(getattr(tenv.user, "session_version", 0)),
            "width": 1024,
            "height": 768,
            "container": None,
        }
    )
    with client.websocket_connect(
        f"/ws/terminal/{tenv.linux.id}?ticket={ticket}"
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "凭据" in msg["data"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4001


def test_ws_device_not_found_closes_4004(tenv):
    # 通过权限复验后，设备不存在 → 4004
    cred_ref = issue_credential(
        {"username": "root", "password": "x", "private_key": ""}
    )
    ticket = issue_ticket(
        {
            "device_id": 999999999,
            "conn_type": "ssh",
            "credential_ref": cred_ref,
            "user_id": tenv.user.id,
            "system_username": tenv.user.username,
            "access_jti": "",
            "session_version": 0,
            "width": 1024,
            "height": 768,
            "container": None,
        }
    )
    with client.websocket_connect(f"/ws/terminal/999999999?ticket={ticket}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "not found" in msg["data"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4004


def test_ws_permission_revoked_closes_4003(tenv):
    # 票据签发后用户被停用 → 复验阶段拒绝（账户已被禁用，4003）
    cred_ref = issue_credential(
        {"username": "root", "password": "x", "private_key": ""}
    )
    ticket = issue_ticket(
        {
            "device_id": tenv.linux.id,
            "conn_type": "ssh",
            "credential_ref": cred_ref,
            "user_id": tenv.user.id,
            "system_username": tenv.user.username,
            "access_jti": "",
            "session_version": 0,
            "width": 1024,
            "height": 768,
            "container": None,
        }
    )
    tenv.user.is_active = 0
    tenv.db.commit()
    try:
        with client.websocket_connect(
            f"/ws/terminal/{tenv.linux.id}?ticket={ticket}"
        ) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4003
    finally:
        tenv.user.is_active = 1
        tenv.db.commit()


def test_decrypt_roundtrip_used_by_terminal():
    """终端凭据回退依赖 crypto.decrypt：密文可还原、损坏返回空串。"""
    assert decrypt(encrypt("secret")) == "secret"
    assert decrypt("corrupt-token") == ""


# ── 上行热路径：只有 mouse/size 需要解析改写 ──


def test_needs_rewrite_detects_mouse_and_size():
    """mouse 要钳位、size 要更新分辨率基准，两者都必须走解析分支。"""
    assert _needs_rdp_rewrite(_build_instruction("mouse", "500", "300", "1"))
    assert _needs_rdp_rewrite(_build_instruction("size", "1024", "768"))
    # 混合报文里只要含 mouse 就得解析
    mixed = _build_instruction("key", "65", "1") + _build_instruction(
        "mouse", "10", "10", "0"
    )
    assert _needs_rdp_rewrite(mixed)


def test_needs_rewrite_short_circuits_bulk_traffic():
    """blob/ack/key/end/sync 占上行流量绝大多数，必须直接透传不解析。

    上传大文件时每条 6KB 的 blob 都要过这里；曾经对同一条指令做三次完整解析
    并重新编码，全部压在事件循环上。
    """
    big_payload = "A" * 8064  # base64 blob 的典型长度
    assert not _needs_rdp_rewrite(_build_instruction("blob", "7", big_payload))
    assert not _needs_rdp_rewrite(_build_instruction("ack", "7", "OK", "0"))
    assert not _needs_rdp_rewrite(_build_instruction("key", "65", "1"))
    assert not _needs_rdp_rewrite(_build_instruction("end", "7"))
    assert not _needs_rdp_rewrite(_build_instruction("sync", "1700000000"))
    assert not _needs_rdp_rewrite(b"")


def test_needs_rewrite_not_fooled_by_base64_payload():
    """base64 字符集不含 '.' 和 ','，载荷无法伪造出 opcode 字面量。

    这保证了嗅探既不会漏判（漏判会让越界鼠标坐标逃过钳位），也不会因为
    载荷内容而误判。
    """
    tricky = "Nb3VzZS4sICJtb3VzZSIsIjUiXQ=="  # 内含 "mouse" 字样的 base64
    assert not _needs_rdp_rewrite(_build_instruction("blob", "7", tricky))


def test_first_mouse_coords_for_log():
    parsed = _build_instruction("mouse", "512", "384", "1")
    assert _first_mouse_coords(parsed) == ("512", "384")
    # 非 mouse 报文或畸形数据只用于日志，必须安全降级而不是抛异常
    assert _first_mouse_coords(_build_instruction("key", "65", "1")) == ("?", "?")
    assert _first_mouse_coords(b"garbage") == ("?", "?")


def test_blob_payload_forwarded_byte_for_byte():
    """透传分支不得改动 blob 字节，否则远端文件会损坏。"""
    payload = "QUJDREVG" * 900
    raw = _build_instruction("blob", "7", payload)
    assert not _needs_rdp_rewrite(raw)
    # 对照：真走解析分支时 blob 也会被原样重建
    rebuilt, w, h, clamped = _sanitize_rdp_input(raw, 1024, 768)
    assert rebuilt == raw
    assert (w, h, clamped) == (1024, 768, 0)
