"""GET /binding 的 os_name 后台补探:存量 binding 的精确版本号自动补齐。

存量绑定(旧代码时期保存)从未触发过凭据探测,os_name 一直为 NULL,
页面「系统」列只能显示「Windows(未获取版本)」。_backfill_os_name
用已存凭据经 WinRM Caption / SSH os-release 补探并落库。
"""

import pytest

from app.database import Base, SessionLocal, engine
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.routers import pve as pve_router
from app.services.crypto import encrypt


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _binding(db, **kw) -> PveGuestBinding:
    conn = PveConnection(
        name="backfill-conn",
        host="10.0.0.1",
        token_id="root@pam!t",
        token_secret_enc="x",
    )
    db.add(conn)
    db.commit()
    fields = dict(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=9999,
        ip_address="192.0.2.9",
        os_system="windows",
        username="administrator",
        password_enc=encrypt("P@ssw0rd"),
        enabled=1,
    )
    fields.update(kw)
    b = PveGuestBinding(**fields)
    db.add(b)
    db.commit()
    return b


def _cleanup(db, b: PveGuestBinding) -> None:
    conn_id = b.connection_id
    db.delete(b)
    db.query(PveConnection).filter_by(id=conn_id).delete()
    db.commit()


def test_backfill_windows_caption(db, monkeypatch):
    b = _binding(db)
    monkeypatch.setattr(
        "app.services.os_detect.probe_windows_os_via_winrm_sync",
        lambda *a, **k: "Microsoft Windows Server 2022 Datacenter",
    )
    try:
        pve_router._backfill_os_name(b.id)
        # 补探在独立会话里提交;先结束当前事务释放 REPEATABLE READ 快照,
        # 否则 refresh 读到的仍是补探前的旧快照。
        db.commit()
        db.refresh(b)
        assert b.os_name == "Microsoft Windows Server 2022 Datacenter"
    finally:
        _cleanup(db, b)


def test_backfill_linux_os_release(db, monkeypatch):
    b = _binding(db, os_system="linux")
    monkeypatch.setattr(
        "app.services.os_detect.probe_linux_os_via_ssh_sync",
        lambda *a, **k: "Debian GNU/Linux 12 (bookworm)",
    )
    try:
        pve_router._backfill_os_name(b.id)
        # 补探在独立会话里提交;先结束当前事务释放 REPEATABLE READ 快照,
        # 否则 refresh 读到的仍是补探前的旧快照。
        db.commit()
        db.refresh(b)
        assert b.os_name == "Debian GNU/Linux 12 (bookworm)"
    finally:
        _cleanup(db, b)


def test_backfill_skips_when_os_name_present(db, monkeypatch):
    b = _binding(db, os_name="已有名称")
    called: list = []
    monkeypatch.setattr(
        "app.services.os_detect.probe_windows_os_via_winrm_sync",
        lambda *a, **k: called.append(1),
    )
    try:
        pve_router._backfill_os_name(b.id)
        # 补探在独立会话里提交;先结束当前事务释放 REPEATABLE READ 快照,
        # 否则 refresh 读到的仍是补探前的旧快照。
        db.commit()
        db.refresh(b)
        assert called == []
        assert b.os_name == "已有名称"
    finally:
        _cleanup(db, b)


def test_backfill_skips_without_credentials(db, monkeypatch):
    b = _binding(db, password_enc=None, ssh_key_enc=None)
    called: list = []
    monkeypatch.setattr(
        "app.services.os_detect.probe_windows_os_via_winrm_sync",
        lambda *a, **k: called.append(1),
    )
    try:
        pve_router._backfill_os_name(b.id)
        # 补探在独立会话里提交;先结束当前事务释放 REPEATABLE READ 快照,
        # 否则 refresh 读到的仍是补探前的旧快照。
        db.commit()
        db.refresh(b)
        assert called == []
        assert b.os_name is None
    finally:
        _cleanup(db, b)


def test_backfill_linux_uses_ssh_key(db, monkeypatch):
    """只配了私钥的 Linux 绑定:补探必须带 private_key,否则永远拿不到精确名。"""
    b = _binding(
        db, os_system="linux", password_enc=None, ssh_key_enc=encrypt("KEYDATA")
    )
    seen: dict = {}

    def _probe(*_a, **k):
        seen.update(k)
        return "Ubuntu 24.04.2 LTS"

    monkeypatch.setattr("app.services.os_detect.probe_linux_os_via_ssh_sync", _probe)
    try:
        pve_router._backfill_os_name(b.id)
        assert seen.get("private_key") == "KEYDATA"
        db.commit()  # 释放 REPEATABLE READ 快照,见上
        db.refresh(b)
        assert b.os_name == "Ubuntu 24.04.2 LTS"
    finally:
        _cleanup(db, b)


def test_backfill_probe_failure_keeps_null(db, monkeypatch):
    b = _binding(db)
    monkeypatch.setattr(
        "app.services.os_detect.probe_windows_os_via_winrm_sync",
        lambda *a, **k: None,
    )
    try:
        pve_router._backfill_os_name(b.id)
        # 补探在独立会话里提交;先结束当前事务释放 REPEATABLE READ 快照,
        # 否则 refresh 读到的仍是补探前的旧快照。
        db.commit()
        db.refresh(b)
        assert b.os_name is None
    finally:
        _cleanup(db, b)
