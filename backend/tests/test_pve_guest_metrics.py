from app.services.pve_guest_metrics import collect_guest_filesystems


def test_qga_filesystems_are_preferred_without_remote_call(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("remote fallback should not run")

    monkeypatch.setattr("app.services.ssh.open_ssh_client", fail)
    monkeypatch.setattr("app.services.winrm.run_powershell", fail)

    result = collect_guest_filesystems(
        {
            "qga_enabled": True,
            "qga_available": True,
            "qga_disks": [
                {
                    "mount": "/",
                    "filesystem_type": "ext4",
                    "total_bytes": 1000,
                    "used_bytes": 250,
                }
            ],
        },
        ip_address="192.0.2.10",
        os_system="linux",
        username="root",
        password="secret",
    )

    assert result["filesystem_source"] == "qga"
    assert result["filesystem_disks"][0]["used_bytes"] == 250


def test_linux_ssh_fallback_uses_df_output(monkeypatch):
    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(
        "app.services.ssh.open_ssh_client",
        lambda *_args, **_kwargs: (FakeClient(), "captured-key"),
    )
    monkeypatch.setattr(
        "app.services.ssh.exec_ssh_command",
        lambda *_args, **_kwargs: (
            0,
            "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
            "/dev/vda1 1000000000 400000000 600000000 40% /\n",
            "",
        ),
    )

    result = collect_guest_filesystems(
        {"qga_enabled": False, "qga_available": False, "qga_disks": []},
        ip_address="192.0.2.11",
        os_system="linux",
        username="root",
        password="secret",
    )

    assert result["filesystem_source"] == "ssh"
    assert result["filesystem_disks"] == [
        {
            "mount": "/",
            "filesystem_type": None,
            "total_bytes": 1000000000,
            "used_bytes": 400000000,
        }
    ]
    assert result["_ssh_host_key"] == "captured-key"


def test_windows_winrm_fallback_uses_logical_disks(monkeypatch):
    monkeypatch.setattr(
        "app.services.winrm.run_powershell",
        lambda *_args, **_kwargs: (0, "disk=C:,1000,700\ndisk=D:,2000,200\n", ""),
    )

    result = collect_guest_filesystems(
        {"qga_enabled": True, "qga_available": False, "qga_disks": []},
        ip_address="192.0.2.12",
        os_system="windows",
        username="Administrator",
        password="secret",
        winrm_port=5985,
    )

    assert result["filesystem_source"] == "winrm"
    assert [disk["mount"] for disk in result["filesystem_disks"]] == ["C:", "D:"]


def test_fallback_reports_missing_binding():
    result = collect_guest_filesystems(
        {"qga_enabled": False, "qga_available": False, "qga_disks": []},
        ip_address=None,
        os_system="linux",
        username=None,
        password=None,
    )

    assert result["filesystem_available"] is False
    assert result["filesystem_source"] is None
    assert "未配置" in result["filesystem_error"]


# ── SSH 兜底走连接池(2026-09-17):复用 transport,TOFU 捕获口径不变 ──


class _FakeTransport:
    def is_active(self):
        return True


class _PoolableFakeClient:
    """带存活 transport 的假 SSHClient,可被 ssh_pool 判定为可复用。"""

    connects = 0

    def __init__(self):
        _PoolableFakeClient.connects += 1

    def get_transport(self):
        return _FakeTransport()

    def close(self):
        pass


def test_linux_ssh_fallback_reuses_pooled_transport(monkeypatch):
    """QGA 不可用的虚机,连续两轮 fs 采集只握手一次(池复用)。"""
    _PoolableFakeClient.connects = 0
    monkeypatch.setattr(
        "app.services.ssh.open_ssh_client",
        lambda *_args, **_kwargs: (_PoolableFakeClient(), "key-b64"),
    )
    monkeypatch.setattr(
        "app.services.ssh.exec_ssh_command",
        lambda *_args, **_kwargs: (
            0,
            "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
            "/dev/vda1 1000 400 600 40% /\n",
            "",
        ),
    )
    from app.services.ssh_pool import ssh_pool

    before = ssh_pool.__len__()

    def _collect():
        return collect_guest_filesystems(
            {"qga_enabled": False, "qga_available": False, "qga_disks": []},
            ip_address="192.0.2.30",
            os_system="linux",
            username="root",
            password="secret",
        )

    r1 = _collect()
    r2 = _collect()
    assert r1["filesystem_source"] == "ssh" and r2["filesystem_source"] == "ssh"
    # 两次采集只新建了一条 transport(池复用)
    assert _PoolableFakeClient.connects == 1
    assert ssh_pool.__len__() == before + 1
    # 清理本用例借出的连接,不影响其它用例的池状态
    key = ssh_pool.key("192.0.2.30", 22, "root", "secret", None) + (None,)
    ssh_pool.discard(key)


def test_linux_ssh_fallback_transport_error_discards_pool_entry(monkeypatch):
    """执行中发生传输层故障:作废池条目,下一次重新握手(而不是借到死连接)。"""
    _PoolableFakeClient.connects = 0
    import paramiko

    monkeypatch.setattr(
        "app.services.ssh.open_ssh_client",
        lambda *_args, **_kwargs: (_PoolableFakeClient(), "key-b64"),
    )

    calls = {"n": 0}

    def flaky_exec(client, cmd, timeout=15):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise paramiko.SSHException("transport dead")
        return (
            0,
            "Filesystem 1B-blocks Used Available Capacity Mounted on\n/dev/x 10 5 5 50% /",
            "",
        )

    monkeypatch.setattr("app.services.ssh.exec_ssh_command", flaky_exec)
    from app.services.ssh_pool import ssh_pool

    base = ssh_pool.__len__()

    def _collect():
        return collect_guest_filesystems(
            {"qga_enabled": False, "qga_available": False, "qga_disks": []},
            ip_address="192.0.2.31",
            os_system="linux",
            username="root",
            password="secret",
        )

    r1 = _collect()  # 传输层故障 → 报错且作废池条目
    assert "transport dead" in (r1.get("filesystem_error") or "")
    assert ssh_pool.__len__() == base  # 已作废
    r2 = _collect()  # 下一次重新握手
    assert r2["filesystem_source"] == "ssh"
    assert _PoolableFakeClient.connects == 2


def test_linux_ssh_fallback_pinned_key_changes_pool_slot(monkeypatch):
    """池键并入 pinned host key:TOFU 落库后,后续采集走强制校验的新连接,
    不再复用 TOFU 时期的那条(未 pin 的池条目)。"""
    _PoolableFakeClient.connects = 0
    monkeypatch.setattr(
        "app.services.ssh.open_ssh_client",
        lambda *_args, **_kwargs: (_PoolableFakeClient(), "captured-b64"),
    )
    monkeypatch.setattr(
        "app.services.ssh.exec_ssh_command",
        lambda *_args, **_kwargs: (
            0,
            "Filesystem 1B-blocks Used Available Capacity Mounted on\n/dev/x 10 5 5 50% /",
            "",
        ),
    )
    from app.services.ssh_pool import ssh_pool

    base = ssh_pool.__len__()

    def _collect(pinned):
        return collect_guest_filesystems(
            {"qga_enabled": False, "qga_available": False, "qga_disks": []},
            ip_address="192.0.2.32",
            os_system="linux",
            username="root",
            password="secret",
            ssh_host_key=pinned,
        )

    r1 = _collect(None)
    assert r1["_ssh_host_key"] == "captured-b64"  # 首次接触捕获
    r2 = _collect("captured-b64")  # pin 之后:不同池键 → 新连接(强制校验)
    assert "_ssh_host_key" not in r2
    assert _PoolableFakeClient.connects == 2
    assert ssh_pool.__len__() == base + 2
    ssh_pool.discard(ssh_pool.key("192.0.2.32", 22, "root", "secret", None) + (None,))
    ssh_pool.discard(
        ssh_pool.key("192.0.2.32", 22, "root", "secret", None) + ("captured-b64",)
    )
