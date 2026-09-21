"""metrics_collector 单元测试：采集编排、凭据解析、计数器求差、历史保留清理。

外部依赖（SSH/WinRM/DB 会话/告警评估）一律用 mock 隔离，绝不真连设备。
用例只断言“自己创建的那几台设备”，对测试库里历史残留的 server/host 行不敏感。
模块级全局缓存（_latest/_prev_net/_prev_io/_last_retention_day）在用例内保存并恢复，
避免污染同一次运行里的其它用例。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.device import Device
from app.models.device_metric_sample import DeviceMetricSample
from app.models.rack import Rack
from app.models.room import Room
from app.services import metrics_collector as mc
from app.services.crypto import encrypt
from app.services.device_credentials import set_credentials

# 与 metrics_parser 测试同款的采集输出样本（解析结果已知，便于断言）。
LINUX_SAMPLE = """@@cpu1
cpu  100 0 50 800 20 0 5 0 0 0
@@cpu2
cpu  110 0 60 900 25 0 6 0 0 0
@@mem
MemTotal:       16384000 kB
MemAvailable:    8192000 kB
@@disk
Filesystem     1B-blocks       Used  Available Capacity Mounted on
/dev/sda1      107374182400 53687091200 48318382080      50% /
@@diskio
   8       0 sda 5000 100 200000 3000 8000 50 400000 6000 0 2000 9000
@@load
0.15 0.10 0.06 1/234 5678
@@uptime
123456.78 234567.89
@@net
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 1000000 100 0 0 0 0 0 0 2000000 200 0 0 0 0 0 0
"""

WINDOWS_SAMPLE = """cpu_pct=23
mem_total_mb=16384
mem_free_mb=8192
uptime_sec=987654
caption=Microsoft Windows Server 2019 Standard
disk=C:,1099511627776,549755813888
netif=Ethernet,1024.5,2048.5
diskio=0 C:,1024,2048,3
"""


# ── DB 设备脚手架（自带清理）──


def _new_rack(db: Session, tag: str) -> tuple[Room, Rack]:
    room = Room(name=f"mc-{tag}-room", location="L1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"mc-{tag}-rack", type="cabinet")
    db.add(rack)
    db.flush()
    return room, rack


def _cleanup(
    db: Session, device_ids: list[int], rack_ids: list[int], room_ids: list[int]
) -> None:
    """按外键顺序回收用例创建的行（含历史样本）。"""
    if device_ids:
        db.query(DeviceMetricSample).filter(
            DeviceMetricSample.device_id.in_(device_ids)
        ).delete(synchronize_session=False)
    db.query(Device).filter(Device.id.in_(device_ids)).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.id.in_(rack_ids)).delete(synchronize_session=False)
    db.query(Room).filter(Room.id.in_(room_ids)).delete(synchronize_session=False)
    db.commit()


def _make_device(db: Session, rack_id: int, **fields) -> Device:
    defaults = dict(type="server", status="online", os_system="Rocky Linux 9.4")
    defaults.update(fields)
    device = Device(rack_id=rack_id, **defaults)
    db.add(device)
    db.commit()
    return device


# ── 纯函数：占位/缓存 ──


def test_unavailable_placeholder_shape():
    entry = mc._unavailable(42, "设备离线")
    assert entry["device_id"] == 42
    assert entry["available"] is False
    assert entry["error"] == "设备离线"
    assert entry["source"] is None
    assert entry["disks"] == []
    assert entry["cpu_pct"] is None


def test_latest_metrics_cache_returns_copy():
    original = mc._latest
    mc._latest = {7: {"device_id": 7, "available": True, "cpu_pct": 1.0}}
    try:
        latest = mc.get_latest_metrics()
        assert latest[7]["cpu_pct"] == 1.0
        # 返回的是拷贝：改它不动内部缓存
        latest[7]["cpu_pct"] = 99.0
        assert mc._latest[7]["cpu_pct"] == 1.0
        assert mc.get_device_metrics(7)["available"] is True
        assert mc.get_device_metrics(999999) is None
    finally:
        mc._latest = original


# ── _fetch_targets ──


def test_fetch_targets_covers_all_managed_types():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "fetch")
    devices: list[Device] = []
    try:
        srv = _make_device(db, rack.id, name="mc-srv", type="server", status="online")
        host = _make_device(db, rack.id, name="mc-host", type="host", status="offline")
        # status="" 应被规整成 "offline"
        blank = _make_device(db, rack.id, name="mc-blank", type="server", status="")
        cloud = _make_device(
            db, rack.id, name="mc-cloud", type="cloud_server", status="online"
        )
        devices = [srv, host, blank, cloud]

        targets = dict(mc._fetch_targets())
        assert targets[srv.id] == "online"
        assert targets[host.id] == "offline"
        assert targets[blank.id] == "offline"
        # 回归:云服务器与物理机走同一套 SSH/WinRM 通道,必须一并采集。
        # 早期这里写死 ("server", "host"),云服务器被静默排除,
        # 连带指标/容器/告警/业务全部看不到它。
        assert targets[cloud.id] == "online"
    finally:
        _cleanup(db, [d.id for d in devices], [rack.id], [room.id])
        db.close()


# ── _resolve_creds ──


class _Dev:
    """带 is_windows 的设备替身（_resolve_creds 只解密设备字段，不碰 DB）。"""

    def __init__(self, username=None, password=None, ssh_key=None, os_system=None):
        self.remote_username = username
        self.remote_password_enc = encrypt(password) if password else None
        self.remote_ssh_key_enc = encrypt(ssh_key) if ssh_key else None
        self.os_system = os_system

    @property
    def is_windows(self) -> bool:
        return "windows" in (self.os_system or "").lower()


def test_resolve_creds_requires_username():
    assert mc._resolve_creds(_Dev(password="p")) is None


def test_resolve_creds_requires_secret():
    # 有用户名但既无密码也无私钥
    assert mc._resolve_creds(_Dev(username="root")) is None


def test_resolve_creds_linux_password_ok():
    out = mc._resolve_creds(_Dev(username="root", password="pw", os_system="linux"))
    assert out == ("root", "pw", "")


def test_resolve_creds_linux_key_only_ok():
    out = mc._resolve_creds(_Dev(username="root", ssh_key="KEY", os_system="linux"))
    assert out == ("root", "", "KEY")


def test_resolve_creds_windows_requires_password():
    # Windows 走 WinRM，必须有密码；仅私钥不算有效
    assert (
        mc._resolve_creds(_Dev(username="admin", ssh_key="KEY", os_system="windows"))
        is None
    )
    out = mc._resolve_creds(_Dev(username="admin", password="pw", os_system="windows"))
    assert out == ("admin", "pw", "")


# ── _collect_linux / _collect_windows ──


class _SshTarget:
    """_collect_linux 用的设备替身(只访问这四个字段,可在会话外使用);
    预置 ssh_host_key 走「已 pin、无需 DB」的池工厂路径。"""

    id = 1
    ip_address = "10.9.0.1"
    ssh_port = 22
    ssh_host_key = "cGlu bmVkIGtleQ=="


@pytest.fixture(autouse=True)
def _fresh_ssh_pool():
    """每个用例前后清空模块级 SSH 池,避免假连接跨用例复用。"""
    mc._ssh_pool.close_all()
    yield
    mc._ssh_pool.close_all()


def test_collect_linux_returns_stdout():
    with (
        patch("app.services.ssh.connect_device", return_value=(MagicMock(), "k")),
        patch("app.services.ssh.exec_ssh_command", return_value=(0, "OUT", "")) as m,
    ):
        assert mc._collect_linux(_SshTarget(), "u", "p", "k") == "OUT"
    m.assert_called_once()


def test_collect_linux_partial_output_kept_despite_nonzero_exit():
    # exit!=0 但有输出 → 仍返回输出（不抛错）
    with (
        patch("app.services.ssh.connect_device", return_value=(MagicMock(), "k")),
        patch("app.services.ssh.exec_ssh_command", return_value=(1, "partial", "warn")),
    ):
        assert mc._collect_linux(_SshTarget(), "u", "p", "") == "partial"


def test_collect_linux_failure_raises():
    with (
        patch("app.services.ssh.connect_device", return_value=(MagicMock(), "k")),
        patch("app.services.ssh.exec_ssh_command", return_value=(1, "", "boom")),
    ):
        with pytest.raises(RuntimeError):
            mc._collect_linux(_SshTarget(), "u", "p", "")


def test_collect_linux_reuses_pooled_transport():
    """同一设备连续两轮采集复用同一条 transport(仅池 miss 时握手一次)。"""
    target = _SshTarget()
    client = MagicMock()
    with (
        patch("app.services.ssh.connect_device", return_value=(client, "k")) as cd,
        patch("app.services.ssh.exec_ssh_command", return_value=(0, "OUT", "")) as ex,
    ):
        assert mc._collect_linux(target, "u", "p", "") == "OUT"
        assert mc._collect_linux(target, "u", "p", "") == "OUT"
    assert cd.call_count == 1  # 握手只在池 miss 时发生一次
    assert ex.call_count == 2
    # 两次执行走的是同一条复用连接
    assert ex.call_args_list[0].args[0] is client
    assert ex.call_args_list[1].args[0] is client


def test_collect_linux_discards_pool_on_transport_error():
    """传输层异常时作废池内连接,让下一轮重建而不是复用僵死 transport。"""
    target = _SshTarget()
    client = MagicMock()
    with (
        patch("app.services.ssh.connect_device", return_value=(client, "k")),
        patch(
            "app.services.ssh.exec_ssh_command",
            side_effect=RuntimeError("broken pipe"),
        ),
    ):
        with pytest.raises(RuntimeError):
            mc._collect_linux(target, "u", "p", "")
    assert len(mc._ssh_pool) == 0


def test_collect_windows_returns_stdout():
    with patch("app.services.winrm.run_on_device", return_value=(0, "WIN", "")):
        assert mc._collect_windows(object(), "u", "p") == "WIN"


def test_collect_windows_failure_raises():
    with patch("app.services.winrm.run_on_device", return_value=(1, "", "boom")):
        with pytest.raises(RuntimeError):
            mc._collect_windows(object(), "u", "p")


# ── _counter_rates ──


def test_counter_rates_none_inputs_skip_baseline():
    prev: dict = {}
    assert mc._counter_rates(prev, 1, None, 5) == (None, None)
    assert mc._counter_rates(prev, 1, 5, None) == (None, None)
    assert prev == {}  # 无有效计数时不写基线


def test_counter_rates_first_then_delta(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock["t"])
    prev: dict = {}
    # 首轮无基线
    assert mc._counter_rates(prev, 1, 100, 50) == (None, None)
    # dt=1s，rx +100 → 100.0，tx +30 → 30.0
    clock["t"] = 1001.0
    assert mc._counter_rates(prev, 1, 200, 80) == (100.0, 30.0)


def test_counter_rates_regression_resets_that_direction(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock["t"])
    prev: dict = {}
    mc._counter_rates(prev, 1, 200, 80)  # 设基线
    clock["t"] = 1001.0
    # rx 回退(设备重启) → None；tx 仍增长 → 20.0
    assert mc._counter_rates(prev, 1, 150, 100) == (None, 20.0)


def test_counter_rates_zero_dt_returns_none(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock["t"])
    prev = {2: (5000.0, 10, 10)}  # 基线时间与当前相同 → dt=0
    assert mc._counter_rates(prev, 2, 20, 20) == (None, None)


# ── _collect_one ──


def test_collect_one_device_not_found():
    entry = mc._collect_one(999999999)
    assert entry["available"] is False
    assert entry["error"] == "设备不存在"


def test_collect_one_various_states_and_linux_success():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "collect")
    created: list[Device] = []
    saved_net = dict(mc._prev_net)
    saved_io = dict(mc._prev_io)
    try:
        no_ip = _make_device(db, rack.id, name="mc-noip", ip_address=None)
        no_cred = _make_device(db, rack.id, name="mc-nocred", ip_address="10.9.1.2")
        linux = _make_device(db, rack.id, name="mc-linux", ip_address="10.9.1.3")
        set_credentials(linux, "root", "pw", None)
        db.commit()
        created = [no_ip, no_cred, linux]

        assert mc._collect_one(no_ip.id)["error"] == "未配置 IP 地址"
        assert mc._collect_one(no_cred.id)["error"] == "未绑定有效凭据"

        # 清掉该设备可能存在的计数器基线，确保首轮速率为 None
        mc._prev_net.pop(linux.id, None)
        mc._prev_io.pop(linux.id, None)
        with (
            patch(
                "app.services.ssh.connect_device", return_value=(MagicMock(), "k")
            ) as cd,
            patch(
                "app.services.ssh.exec_ssh_command", return_value=(0, LINUX_SAMPLE, "")
            ),
        ):
            entry = mc._collect_one(linux.id)

        assert entry["available"] is True
        assert entry["source"] == "ssh"
        # 无 pinned host key → 池工厂走首次 TOFU 路径(带 db 连接并回写 key)
        assert cd.call_args.kwargs.get("db") is not None
        assert entry["cpu_pct"] == 16.7
        assert entry["mem_pct"] == 50.0
        assert entry["mem_used_mb"] == 8000
        assert entry["disk_max_pct"] == 50.0
        assert entry["load1"] == 0.15
        assert entry["uptime_sec"] == 123456
        # 首轮无网络基线 → 速率为 None（但累计计数器已写入基线）
        assert entry["net_rx_bps"] is None
        # 历史样本随条目一起带出，供批量落库
        sample = entry["_sample"]
        assert sample["device_id"] == linux.id
        assert sample["cpu_pct"] == 16.7
        assert sample["disks_json"]
    finally:
        for did in [d.id for d in created]:
            mc._prev_net.pop(did, None)
            mc._prev_io.pop(did, None)
        mc._prev_net.update(saved_net)
        mc._prev_io.update(saved_io)
        _cleanup(db, [d.id for d in created], [rack.id], [room.id])
        db.close()


def test_collect_one_windows_self_heals_os_system():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "win")
    win = None
    try:
        win = _make_device(
            db,
            rack.id,
            name="mc-win",
            ip_address="10.9.2.1",
            os_system="windows",
            type="host",
        )
        set_credentials(win, "admin", "pw", None)
        db.commit()

        with patch(
            "app.services.winrm.run_on_device", return_value=(0, WINDOWS_SAMPLE, "")
        ):
            entry = mc._collect_one(win.id)

        assert entry["available"] is True
        assert entry["source"] == "winrm"
        assert entry["cpu_pct"] == 23.0
        # Windows 计数器是直读速率，无需跨周期基线
        assert entry["net_rx_bps"] == 1024.5
        assert entry["disk_read_bps"] == 1024.0

        # os_system 从笼统的 "windows" 自愈成精确 Caption
        db.rollback()  # 结束当前事务，重开快照才能看到其它会话的提交
        reloaded = db.query(Device).filter(Device.id == win.id).first()
        assert reloaded.os_system == "Microsoft Windows Server 2019 Standard"
    finally:
        _cleanup(db, [win.id] if win else [], [rack.id], [room.id])
        db.close()


def test_collect_one_parse_failure_and_ssh_exception():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "fail")
    dev = None
    try:
        dev = _make_device(db, rack.id, name="mc-fail", ip_address="10.9.3.1")
        set_credentials(dev, "root", "pw", None)
        db.commit()

        # 输出无法解析（ok=False）
        with (
            patch("app.services.ssh.connect_device", return_value=(MagicMock(), "k")),
            patch("app.services.ssh.exec_ssh_command", return_value=(0, "garbage", "")),
        ):
            assert mc._collect_one(dev.id)["error"] == "指标解析失败"

        # 底层 SSH 抛异常 → 降级为不可用，错误信息被截断保留
        with (
            patch("app.services.ssh.connect_device", return_value=(MagicMock(), "k")),
            patch(
                "app.services.ssh.exec_ssh_command",
                side_effect=RuntimeError("conn refused"),
            ),
        ):
            entry = mc._collect_one(dev.id)
        assert entry["available"] is False
        assert "conn refused" in entry["error"]
    finally:
        _cleanup(db, [dev.id] if dev else [], [rack.id], [room.id])
        db.close()


# ── _cleanup_retention ──


def test_cleanup_retention_deletes_only_old_samples():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "retention")
    dev = None
    saved_day = mc._last_retention_day
    old_id = new_id = None
    try:
        dev = _make_device(db, rack.id, name="mc-retention", ip_address="10.9.4.1")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old = DeviceMetricSample(
            device_id=dev.id, ts=now - timedelta(days=30), cpu_pct=1.0
        )
        new = DeviceMetricSample(device_id=dev.id, ts=now, cpu_pct=2.0)
        db.add_all([old, new])
        db.commit()
        old_id, new_id = old.id, new.id

        mc._last_retention_day = None  # 强制本轮执行
        mc._cleanup_retention()

        today = datetime.now(timezone.utc).date().isoformat()
        assert mc._last_retention_day == today
        db.rollback()  # 结束当前事务，重开快照才能看到其它会话的提交
        assert db.query(DeviceMetricSample).filter_by(id=old_id).first() is None
        assert db.query(DeviceMetricSample).filter_by(id=new_id).first() is not None

        # 同一天再次调用直接早退，不报错
        mc._cleanup_retention()
        assert mc._last_retention_day == today
    finally:
        mc._last_retention_day = saved_day
        _cleanup(db, [dev.id] if dev else [], [rack.id], [room.id])
        db.close()


def test_cleanup_retention_deletes_in_batches(monkeypatch):
    """分批删除:批大小小于旧行总数时多批循环删完(短事务摊锁/undo 压力)。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "batch")
    dev = None
    saved_day = mc._last_retention_day
    try:
        dev = _make_device(db, rack.id, name="mc-batch", ip_address="10.9.5.1")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add_all(
            DeviceMetricSample(
                device_id=dev.id, ts=now - timedelta(days=30), cpu_pct=1.0
            )
            for _ in range(5)
        )
        db.commit()

        monkeypatch.setattr(mc, "_RETENTION_BATCH_ROWS", 2)
        mc._last_retention_day = None
        mc._cleanup_retention()

        db.rollback()
        remaining = (
            db.query(DeviceMetricSample)
            .filter(DeviceMetricSample.device_id == dev.id)
            .count()
        )
        assert remaining == 0  # 5 行、批大小 2 → 3 批全部删完
    finally:
        mc._last_retention_day = saved_day
        _cleanup(db, [dev.id] if dev else [], [rack.id], [room.id])
        db.close()


def test_collect_cycle_prunes_counter_baselines_for_removed_devices():
    """已删设备的计数器基线不能无限残留(内存防泄漏)。"""
    saved_net = dict(mc._prev_net)
    saved_io = dict(mc._prev_io)
    saved_latest = mc._latest
    fake_db = MagicMock()
    try:
        # 设备 77/78 的基线残留;本轮目标只剩 78(在线)与 79(离线)
        mc._prev_net[77] = (1000.0, 10, 10)
        mc._prev_net[78] = (1000.0, 20, 20)
        mc._prev_io[77] = (1000.0, 30, 30)
        with (
            patch.object(
                mc, "_fetch_targets", return_value=[(78, "online"), (79, "offline")]
            ),
            patch.object(
                mc, "_collect_one", return_value={"device_id": 78, "available": True}
            ),
            patch.object(mc, "SessionLocal", return_value=fake_db),
            patch.object(mc, "_cleanup_retention"),
            patch.object(mc, "evaluate_alerts"),
            patch.object(mc, "evaluate_host_status_alerts"),
            # 业务告警评估已迁至独立循环(run_business_alert_loop,2026-09-17),
            # 指标周期不再触碰它——这里不再需要 patch 兑底。
            patch.object(mc, "close_orphaned_alert_events"),
        ):
            asyncio.run(mc._collect_cycle_unlocked())
        assert 77 not in mc._prev_net
        assert 77 not in mc._prev_io
        assert 78 in mc._prev_net  # 本轮目标保留
    finally:
        mc._prev_net.clear()
        mc._prev_net.update(saved_net)
        mc._prev_io.clear()
        mc._prev_io.update(saved_io)
        mc._latest = saved_latest


# ── 异步编排：trigger_collection / _collect_cycle ──


def test_trigger_collection_orchestrates_and_persists():
    saved_latest = mc._latest
    fake_entry = {
        "device_id": 1,
        "available": True,
        "error": None,
        "source": "ssh",
        "cpu_pct": 12.5,
        "_sample": {"device_id": 1, "cpu_pct": 12.5},
    }
    fake_db = MagicMock()
    try:
        with (
            patch.object(
                mc, "_fetch_targets", return_value=[(1, "online"), (2, "offline")]
            ),
            patch.object(mc, "_collect_one", return_value=fake_entry),
            patch.object(mc, "SessionLocal", return_value=fake_db),
            patch.object(mc, "_cleanup_retention") as retention,
            patch.object(mc, "evaluate_alerts") as eval_alerts,
            patch.object(mc, "evaluate_host_status_alerts"),
            # 业务告警评估已迁至独立循环(run_business_alert_loop,2026-09-17),
            # 指标周期不再触碰它。
            patch.object(mc, "close_orphaned_alert_events") as orphan,
        ):
            result = asyncio.run(mc.trigger_collection())

        # 在线设备走采集结果，离线设备落占位
        assert result[1]["available"] is True
        assert result[1]["cpu_pct"] == 12.5
        assert result[2]["available"] is False
        assert result[2]["error"] == "设备离线"
        # 带 _sample 的条目触发批量落库
        fake_db.bulk_insert_mappings.assert_called_once()
        fake_db.commit.assert_called()
        # 每轮收尾的告警评估都被调用
        eval_alerts.assert_called_once()
        orphan.assert_called_once()
        retention.assert_called_once()
    finally:
        mc._latest = saved_latest
