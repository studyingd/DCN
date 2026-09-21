"""在线探测(monitor)的设备类型相关行为。

重点覆盖「哨兵端口守卫」对云服务器的豁免：云上 SLB/NAT/安全组会对任意端口
应答握手，守卫会把健康的云服务器判成假在线进而标为 offline，导致自动化运维
面板的 deviceSelectable(status === 'online') 把它变成不可选。

异步部分统一用 asyncio.run 包在同步用例里，与 tests/test_os_detect.py、
tests/test_background_leader.py 保持一致（本仓库未安装 pytest-asyncio）。
"""

import asyncio
import inspect

import pytest

from app.services import monitor


class _FakeSession:
    """_run_scan_unlocked 只调用 close()，查询全部被 monkeypatch 掉。"""

    def close(self) -> None:
        pass


# (id, ip, ssh_port, rdp_port, winrm_port, os_system, status, type)
def _row(
    device_id: int, device_type: str, os_system: str = ""
) -> tuple[int, str, int, int, int, str, str, str]:
    return (
        device_id,
        f"10.0.0.{device_id}",
        22,
        3389,
        5985,
        os_system,
        "offline",
        device_type,
    )


@pytest.fixture
def stub(monkeypatch):
    """把探测与 DB 全部替换成可控桩，返回用于设置桩行为的命名空间。"""

    class Stub:
        rows: list[tuple] = []
        sentinel_open = True  # 哨兵端口是否应答(_is_real_host 判假的依据)
        mgmt_open = True  # 管理端口是否可达
        probed: list[int] = []  # 实际探测过的端口顺序
        updated: dict[int, str] | None = None

    s = Stub()

    async def fake_port_open(host, port, timeout):
        return s.sentinel_open

    async def fake_check_tcp_port(host, port, timeout):
        s.probed.append(port)
        return s.mgmt_open

    def fake_update(db, updates):
        s.updated = dict(updates)

    monkeypatch.setattr(monitor, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(monitor, "_load_devices", lambda db: list(s.rows))
    monkeypatch.setattr(monitor, "_port_open", fake_port_open)
    monkeypatch.setattr(monitor, "_check_tcp_port", fake_check_tcp_port)
    monkeypatch.setattr(monitor, "_update_device_statuses", fake_update)
    # 扫描结果写在全局缓存里，测试之间必须隔离。
    monkeypatch.setattr(monitor, "_latest_statuses", {})
    monkeypatch.setattr(monitor, "_fail_counts", {})
    return s


def _scan() -> dict[int, str]:
    return asyncio.run(monitor._run_scan_unlocked())


# ── _host_guard_applies ──


def test_host_guard_applies_to_physical_devices():
    assert monitor._host_guard_applies("server") is True
    assert monitor._host_guard_applies("host") is True


def test_host_guard_skips_cloud_server():
    assert monitor._host_guard_applies("cloud_server") is False


def test_host_guard_is_case_and_space_tolerant():
    assert monitor._host_guard_applies("Cloud_Server") is False
    assert monitor._host_guard_applies("  cloud_server ") is False
    # 空/未知类型保守处理：仍然执行守卫
    assert monitor._host_guard_applies("") is True
    assert monitor._host_guard_applies(None) is True


# ── _load_devices 必须带出 type ──


def test_load_devices_selects_type_column():
    """守卫按类型豁免；_load_devices 少查 type 会让豁免静默失效。"""
    src = inspect.getsource(monitor._load_devices)
    assert "Device.type" in src
    # 返回 8 元组：id/ip/ssh/rdp/winrm/os_system/status/type
    assert "list[tuple[int, str, int, int, int, str, str, str]]" in src


# ── 扫描行为 ──


def test_cloud_server_online_even_when_sentinels_answer(stub):
    """回归：哨兵端口全通时，云服务器仍按管理端口判定为 online。"""
    stub.rows = [_row(1, "cloud_server")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {1: "online"}
    assert monitor.get_latest_statuses() == {1: "online"}


def test_physical_server_guarded_against_syn_answering_firewall(stub):
    """物理机的守卫保持不变：哨兵端口全通即判为假在线 → offline。"""
    stub.rows = [_row(2, "server")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {2: "offline"}


def test_physical_host_guarded_too(stub):
    stub.rows = [_row(3, "host")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {3: "offline"}


def test_cloud_server_still_offline_when_management_port_closed(stub):
    """豁免守卫不等于无条件 online：管理端口不通仍然 offline。"""
    stub.rows = [_row(4, "cloud_server")]
    stub.sentinel_open = True
    stub.mgmt_open = False

    assert _scan() == {4: "offline"}


def test_cloud_server_online_when_sentinels_refuse(stub):
    """哨兵端口正常拒绝时，云服务器与物理机一样判 online。"""
    stub.rows = [_row(5, "cloud_server")]
    stub.sentinel_open = False
    stub.mgmt_open = True

    assert _scan() == {5: "online"}


def test_windows_cloud_server_probes_winrm_first(stub):
    """Windows 云服务器主探 WinRM(5985)，而不是 SSH 22。

    os_system 为空时会退化去探 22 —— 这正是 backfill_os_system.py 要补齐的字段。
    """
    stub.rows = [_row(6, "cloud_server", os_system="Microsoft Windows Server 2019")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {6: "online"}
    assert stub.probed[0] == 5985


def test_linux_cloud_server_probes_ssh_first(stub):
    stub.rows = [_row(7, "cloud_server", os_system="Ubuntu 22.04")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {7: "online"}
    assert stub.probed[0] == 22


def test_device_without_ip_is_offline(stub):
    row = _row(8, "cloud_server")
    stub.rows = [(row[0], "", *row[2:])]

    assert _scan() == {8: "offline"}
    # 无 IP 直接短路，不该发起任何端口探测
    assert stub.probed == []


def test_mixed_fleet_scanned_together(stub):
    """同一轮扫描里三种类型互不影响。"""
    stub.rows = [
        _row(10, "server"),
        _row(11, "cloud_server"),
        _row(12, "host"),
    ]
    stub.sentinel_open = True
    stub.mgmt_open = True

    assert _scan() == {10: "offline", 11: "online", 12: "offline"}


def test_status_change_is_persisted(stub):
    """状态变化要写库，否则 devices.status 不会更新。"""
    stub.rows = [_row(13, "cloud_server")]
    stub.sentinel_open = True
    stub.mgmt_open = True

    _scan()

    assert stub.updated == {13: "online"}


# ── 离线消抖：单次失败不翻灯，连续 2 次才翻 ──


def test_single_probe_failure_keeps_online(stub):
    """在线设备单次探测失败(网络抖动)不立即翻 offline。"""
    stub.rows = [_row(21, "cloud_server")]
    stub.mgmt_open = True
    _scan()  # 第 1 轮:online
    assert stub.updated == {21: "online"}  # 首轮的 offline→online 正常落库

    stub.updated = None
    stub.mgmt_open = False
    _scan()  # 第 2 轮:单次失败 → 保持 online

    assert monitor.get_latest_statuses()[21] == "online"
    assert stub.updated is None  # 首次失败不写库、不翻灯


def test_second_consecutive_failure_flips_offline(stub):
    """连续第 2 次失败才确认离线。"""
    stub.rows = [_row(22, "cloud_server")]
    stub.mgmt_open = True
    _scan()

    stub.mgmt_open = False
    _scan()  # 失败 1 次:保持 online
    _scan()  # 失败 2 次:翻 offline

    assert monitor.get_latest_statuses()[22] == "offline"
    assert stub.updated == {22: "offline"}


def test_recovery_is_immediate(stub):
    """离线设备单次探测成功立即恢复 online(失败计数同步清零)。"""
    stub.rows = [_row(23, "cloud_server")]
    stub.mgmt_open = False
    _scan()  # 失败 1
    _scan()  # 失败 2 → offline

    stub.mgmt_open = True
    _scan()  # 单次成功 → 立即 online

    assert monitor.get_latest_statuses()[23] == "online"


def test_intermittent_failure_never_flips(stub):
    """失败/成功交替时永远不翻离线(交替抖动不产生闪烁)。"""
    stub.rows = [_row(24, "cloud_server")]
    stub.mgmt_open = True
    _scan()
    for _ in range(4):  # 失败/成功交替 4 轮
        stub.mgmt_open = False
        _scan()
        stub.mgmt_open = True
        _scan()
    assert monitor.get_latest_statuses()[24] == "online"


def test_fail_counts_pruned_for_deleted_devices(stub):
    """设备删除后失败计数不能无限残留(内存防泄漏)。"""
    stub.rows = [_row(31, "cloud_server")]
    stub.mgmt_open = False
    _scan()  # 失败 1 次 → 计数入表
    assert 31 in monitor._fail_counts

    stub.rows = [_row(32, "cloud_server")]  # 设备 31 已删除
    stub.mgmt_open = True
    _scan()

    assert 31 not in monitor._fail_counts
    assert 32 not in monitor._fail_counts  # 成功即清零
