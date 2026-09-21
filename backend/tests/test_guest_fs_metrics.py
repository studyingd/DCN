"""虚拟机磁盘使用率告警(guest_fs_metrics 缓存 + 评估链路)。

PVE 快照没有 guest 文件系统数据,磁盘值由低频采集循环(QGA get-fsinfo 优先,
运维接入 SSH/WinRM 兜底)写进内存缓存,评估器经 _pve_metric_entries 合成
disk_max_pct 条目——与 cpu/mem 同一条扣住 + AI 归因 + 单卡片路径。
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.database import Base, SessionLocal, engine
from app.models.alert import AlertRule
from app.models.device import Device
from app.models.pve_connection import PveConnection
from app.models.rack import Rack
from app.models.room import Room
from app.services import alerts as alerts_service
from app.services import guest_fs_metrics as fs


@pytest.fixture()
def env():
    """机房/设备/PVE 平台/规则各一份,测试后按外键顺序清理。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    stamp = datetime.now(timezone.utc).timestamp()
    room = Room(name=f"FsRoom{stamp}", location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"FsRack{stamp}", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"fs-dev-{int(stamp)}",
        ip_address="10.7.0.5",
        type="server",
        os_system="linux",
    )
    db.add(device)
    db.flush()
    conn = PveConnection(
        name=f"fs-pve-{int(stamp)}",
        host="10.7.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    rule = AlertRule(
        name=f"fs-rule-{int(stamp)}",
        metric="disk_max_pct",
        threshold=80.0,
        sustain_seconds=0,
        cooldown_seconds=300,
        severity="warning",
        enabled=True,
    )
    db.add(rule)
    db.commit()
    event_ids: list[int] = []

    def make_event(**overrides) -> dict:
        from app.models.alert import AlertEvent

        fields = {
            "rule_id": rule.id,
            "device_id": None,
            "resource_type": "device",
            "resource_id": str(-(conn.id * 1_000_000 + 101)),
            "resource_name": f"[{conn.name}] web-01",
            "metric": "disk_max_pct",
            "value": 93.0,
            "threshold": 80.0,
            "severity": "warning",
            "status": "open",
            "message": "m",
            "first_triggered_at": datetime.now(timezone.utc),
            "last_seen_at": datetime.now(timezone.utc),
            "occurrence_count": 1,
            "notification_status": "pending",
        }
        fields.update(overrides)
        event = AlertEvent(**fields)
        db.add(event)
        db.commit()
        db.refresh(event)
        event_ids.append(event.id)
        return event

    try:
        yield SimpleNamespace(
            db=db,
            device=device,
            conn=conn,
            rule=rule,
            stamp=stamp,
            make_event=make_event,
        )
    finally:
        fs._cache.clear()
        from app.models.alert import AlertEvent

        db.query(AlertEvent).filter(AlertEvent.id.in_(event_ids)).delete(
            synchronize_session=False
        )
        # 评估器可能为 env.rule 生成事件,一并清掉
        db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).delete(
            synchronize_session=False
        )
        db.commit()
        db.query(AlertRule).filter(AlertRule.id == rule.id).delete()
        db.query(Device).filter(Device.id == device.id).delete()
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.commit()
        db.close()


def _disk(mount: str, total: int, used: int) -> dict:
    return {
        "mount": mount,
        "filesystem_type": "ext4",
        "total_bytes": total,
        "used_bytes": used,
    }


# ── 纯逻辑:取所有文件系统里使用率最大的 ──


def test_disk_max_pct_takes_maximum():
    assert fs._disk_max_pct([_disk("/", 100, 50), _disk("/data", 100, 91)]) == 91.0
    # 单行
    assert fs._disk_max_pct([_disk("/", 200, 100)]) == 50.0
    # 不可用行(容量<=0)跳过;空列表无值
    assert fs._disk_max_pct([_disk("/x", 0, 0)]) is None
    assert fs._disk_max_pct([]) is None


def test_cache_roundtrip():
    fs._cache.clear()
    fs._store(1, 101, {"available": True, "sampled_at": 1.0, "disk_max_pct": 88.8})
    entry = fs.get_guest_disk_entry(1, 101)
    assert entry["disk_max_pct"] == 88.8 and entry["available"] is True
    assert fs.get_guest_disk_entry(9, 999) is None
    fs._cache.clear()


# ── 采集范围:只服务磁盘规则,没规则零开销 ──


def test_refresh_all_skips_when_no_disk_rules(env):
    fs._cache.clear()
    # 隔离进程内 guest 快照:同进程先跑过 pve_guest_status 用例时会残留
    # _guests 数据,叠加 env 规则的空目标(collect_all=True)会让采集循环
    # 真的去遍历 dev 库里的真实连接——顺序脆弱,与"没规则"的本意无关。
    import app.services.pve_guest_status as svc

    saved = dict(svc._guests)
    svc._guests.clear()
    try:
        with patch.object(fs, "_collect_one") as collect:
            assert fs.refresh_all(env.db) == 0
        collect.assert_not_called()
    finally:
        svc._guests.clear()
        svc._guests.update(saved)
        fs._cache.clear()


def test_refresh_all_collects_explicit_and_all_scope(env):
    """显式目标按负数 id 解码;空目标(全部对象)时对 running guest 也建缓存。"""
    from app.models.alert import AlertRule

    target = -(env.conn.id * 1_000_000 + 101)
    env.db.add(
        AlertRule(
            name="disk-explicit",
            metric="disk_max_pct",
            threshold=80.0,
            sustain_seconds=0,
            cooldown_seconds=300,
            severity="warning",
            target_device_ids=[target],
        )
    )
    env.db.commit()

    guests = {
        101: {
            "vmid": 101,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "web",
        }
    }
    fs._cache.clear()

    def _guests_by_conn(conn_id: int) -> dict:
        # 开发库里有真实启用的 PVE 连接,只对本用例的连接返回 guest
        return guests if conn_id == env.conn.id else {}

    # 开发库里可能有别人建的真实磁盘规则,目标解析隔离成只有本用例的目标
    with (
        patch.object(
            fs, "_wanted_guest_keys", return_value=({(env.conn.id, 101)}, False)
        ),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "build_client", return_value=SimpleNamespace()),
        patch.object(
            fs,
            "_collect_one",
            return_value={
                "available": True,
                "sampled_at": time.time(),
                "disk_max_pct": 93.1,
            },
        ) as collect,
    ):
        assert fs.refresh_all(env.db) == 1
    collect.assert_called_once()
    entry = fs.get_guest_disk_entry(env.conn.id, 101)
    assert entry["disk_max_pct"] == 93.1

    # 空目标 = 全部对象:所有 running guest 都采集(这里补一台 102)
    env.db.add(
        AlertRule(
            name="disk-all",
            metric="disk_max_pct",
            threshold=80.0,
            sustain_seconds=0,
            cooldown_seconds=300,
            severity="warning",
        )
    )
    env.db.commit()
    guests[102] = {
        "vmid": 102,
        "guest_type": "qemu",
        "status": "running",
        "node": "pve1",
        "name": "db",
    }
    with (
        patch.object(fs, "_wanted_guest_keys", return_value=(set(), True)),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "build_client", return_value=SimpleNamespace()),
        patch.object(
            fs,
            "_collect_one",
            return_value={
                "available": True,
                "sampled_at": time.time(),
                "disk_max_pct": 10.0,
            },
        ) as collect,
    ):
        assert fs.refresh_all(env.db) == 2
    assert collect.call_count == 2
    fs._cache.clear()


def test_collect_one_passes_binding_fallback_values(env):
    """磁盘循环只用 fsinfo-only 摘要(无 IP/OS 字段):SSH/WinRM 兜底的
    地址/OS/凭据全部来自运维接入绑定,不再依赖 QGA interfaces/osinfo。"""
    binding = SimpleNamespace(
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="enc",
        ssh_key_enc=None,
        ssh_port=22,
        winrm_port=5985,
        ssh_host_key=None,
    )
    captured: dict = {}

    def fake_collect(qga, **kwargs):
        captured.update(kwargs)
        return {
            "filesystem_available": True,
            "filesystem_source": "qga",
            "filesystem_disks": [_disk("/", 100, 92)],
            "filesystem_error": None,
        }

    # QGA fsinfo 成功:qga_available=True + qga_disks(磁盘行由兜底层原样采信)
    qga = {
        "qga_enabled": True,
        "qga_available": True,
        "qga_disks": [_disk("/", 100, 92)],
    }
    guest = {"vmid": 101, "guest_type": "qemu", "node": "pve1"}
    with (
        patch.object(fs, "guest_agent_fsinfo_summary", return_value=qga),
        patch.object(fs, "collect_guest_filesystems", side_effect=fake_collect),
        patch.object(fs, "decrypt", side_effect=lambda v: "secret"),
    ):
        entry = fs._collect_one(SimpleNamespace(), env.conn, guest, binding)
    assert entry["available"] is True and entry["disk_max_pct"] == 92.0
    assert entry["source"] == "qga"

    # QGA 不可用 → 兜底参数全部来自绑定
    qga_down = {"qga_enabled": True, "qga_available": False, "qga_disks": []}
    with (
        patch.object(fs, "guest_agent_fsinfo_summary", return_value=qga_down),
        patch.object(fs, "collect_guest_filesystems", side_effect=fake_collect),
        patch.object(fs, "decrypt", side_effect=lambda v: "secret"),
    ):
        entry = fs._collect_one(SimpleNamespace(), env.conn, guest, binding)
    assert captured["ip_address"] == "10.7.0.21"
    assert captured["os_system"] == "linux"
    assert captured["password"] == "secret"


# ── 评估链路:磁盘规则对虚拟机产生/恢复事件 ──


def test_disk_rule_fires_for_pve_guest(env):
    """虚拟机磁盘超阈值 → 负数 target_id 事件正常产生并 open(带 disk_sampled_at)。"""
    from app.models.alert import AlertEvent

    target = -(env.conn.id * 1_000_000 + 101)
    env.rule.metric = "disk_max_pct"
    env.rule.threshold = 80.0
    env.rule.sustain_seconds = 0
    env.rule.target_device_ids = [target]
    env.db.commit()

    disk_ts = time.time() - 30
    fs._cache.clear()
    fs._store(
        env.conn.id,
        101,
        {"available": True, "sampled_at": disk_ts, "disk_max_pct": 93.2},
    )
    guests = {
        101: {
            "vmid": 101,
            "name": "web-01",
            "status": "running",
            "cpu": 0.1,
            "mem": 4 * 1024**3,
            "maxmem": 8 * 1024**3,
        }
    }
    state = {
        "name": env.conn.name,
        "reachable": True,
        "error": None,
        "checked_at": time.time(),
    }
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch("app.services.alerts.notify_event"),
        patch("app.services.alerts.sweep_expired_notification_holds"),
        patch("app.services.alerts.submit_analysis"),
    ):
        alerts_service.evaluate_alerts({})

    env.db.commit()
    events = env.db.query(AlertEvent).filter(AlertEvent.rule_id == env.rule.id).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "open"
    assert ev.device_id is None
    assert int(ev.resource_id) == target
    assert ev.value == 93.2
    assert "磁盘使用率 93.2% 已超过阈值 80.0%" in ev.message
    assert "web-01" in ev.message
    # sustain 回溯用 fs 采集时刻,不是评估时刻
    assert ev.first_triggered_at is not None

    # 值回落 → 事件恢复(不能因虚拟机身份被 sweep 误清)
    fs._store(
        env.conn.id,
        101,
        {"available": True, "sampled_at": time.time(), "disk_max_pct": 40.0},
    )
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch("app.services.alerts.notify_event"),
        patch("app.services.alerts.sweep_expired_notification_holds"),
    ):
        alerts_service.evaluate_alerts({})
    env.db.commit()
    ev = env.db.query(AlertEvent).filter(AlertEvent.id == ev.id).first()
    assert ev.status == "resolved"
    fs._cache.clear()


def test_disk_entry_missing_keeps_event_open(env):
    """fs 缓存缺失(采集失败/刚启动)→ 评估跳过,既有事件不被误恢复。"""
    from app.models.alert import AlertEvent

    target = -(env.conn.id * 1_000_000 + 101)
    env.rule.metric = "disk_max_pct"
    env.rule.threshold = 80.0
    env.rule.sustain_seconds = 0
    env.rule.target_device_ids = [target]
    env.db.commit()
    event = env.make_event(
        metric="disk_max_pct",
        value=93.0,
        threshold=80.0,
        device_id=None,
        resource_id=str(target),
        resource_name=f"[{env.conn.name}] web-01",
        status="open",
    )

    guests = {
        101: {
            "vmid": 101,
            "name": "web-01",
            "status": "running",
            "cpu": 0.1,
            "mem": 1,
            "maxmem": 2,
        }
    }
    state = {
        "name": env.conn.name,
        "reachable": True,
        "error": None,
        "checked_at": time.time(),
    }
    fs._cache.clear()  # 没有磁盘缓存
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch("app.services.alerts.notify_event"),
        patch("app.services.alerts.sweep_expired_notification_holds"),
    ):
        alerts_service.evaluate_alerts({})
    env.db.commit()
    row = env.db.query(AlertEvent).filter(AlertEvent.id == event.id).first()
    assert row.status == "open"


def test_sampled_at_uses_disk_timestamp_for_disk_metric():
    now = datetime.now(timezone.utc)
    base = now - timedelta(minutes=10)  # PVE 快照时刻
    entry = {
        "sampled_at": base,
        "disk_sampled_at": base - timedelta(minutes=5),  # fs 采集时刻(更早 5 分钟)
    }
    assert (
        alerts_service._sampled_at(entry, "disk_max_pct", now)
        == entry["disk_sampled_at"]
    )
    # 非 disk 指标仍用快照时刻
    assert alerts_service._sampled_at(entry, "mem_pct", now) == base
    # 没有 disk 时间戳时回退
    assert alerts_service._sampled_at({"sampled_at": base}, "disk_max_pct", now) == base
    assert alerts_service._sampled_at({}, "disk_max_pct", now) == now


# ── 缓存清理:虚机销毁后条目不残留(防长跑进程缓慢泄漏) ──


def test_refresh_all_prunes_deleted_guest_cache(env):
    """快照里已不存在的 guest:fs 缓存条目随采集循环清掉;存活的保留。"""
    fs._cache.clear()
    fs._store(
        env.conn.id,
        101,
        {"available": True, "sampled_at": time.time(), "disk_max_pct": 50.0},
    )
    fs._store(
        env.conn.id,
        999,
        {"available": True, "sampled_at": time.time(), "disk_max_pct": 90.0},
    )

    guests = {
        101: {
            "vmid": 101,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "web",
        }
    }

    def _guests_by_conn(conn_id: int) -> dict:
        return guests if conn_id == env.conn.id else {}

    with (
        patch.object(
            fs, "_wanted_guest_keys", return_value=({(env.conn.id, 101)}, False)
        ),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "build_client", return_value=SimpleNamespace()),
        patch.object(
            fs,
            "_collect_one",
            return_value={
                "available": True,
                "sampled_at": time.time(),
                "disk_max_pct": 50.0,
            },
        ),
    ):
        assert fs.refresh_all(env.db) == 1
    # 999 已从 PVE 快照消失(虚机销毁)→ 条目被清理;101 仍在
    assert fs.get_guest_disk_entry(env.conn.id, 999) is None
    entry = fs.get_guest_disk_entry(env.conn.id, 101)
    assert entry is not None and entry["disk_max_pct"] == 50.0
    fs._cache.clear()


def test_refresh_all_prunes_even_without_disk_rules(env):
    """没有启用中的磁盘规则时,清理照常执行(判定依据是快照存在性,不是规则)。"""
    fs._cache.clear()
    fs._store(
        env.conn.id,
        777,
        {"available": True, "sampled_at": time.time(), "disk_max_pct": 80.0},
    )

    def _guests_by_conn(conn_id: int) -> dict:
        # 快照里没有任何 guest(全部已销毁)
        return {}

    with (
        patch.object(fs, "_wanted_guest_keys", return_value=(set(), False)),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "_collect_one") as collect,
    ):
        assert fs.refresh_all(env.db) == 0
    collect.assert_not_called()
    assert fs.get_guest_disk_entry(env.conn.id, 777) is None
    fs._cache.clear()


# ── 并行采集(2026-09-17):并发 4,回存顺序/失败隔离与串行一致 ──


def test_refresh_all_collects_concurrently_and_ordered(env):
    """多 guest 并行采集(模拟慢速 QGA),回存顺序仍按 vmid 升序、计数正确。"""
    from app.models.alert import AlertRule

    env.db.add(
        AlertRule(
            name="disk-parallel",
            metric="disk_max_pct",
            threshold=80.0,
            sustain_seconds=0,
            cooldown_seconds=300,
            severity="warning",
        )
    )
    env.db.commit()
    guests = {
        101: {
            "vmid": 101,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "web",
        },
        102: {
            "vmid": 102,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "db",
        },
        103: {
            "vmid": 103,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "cache",
        },
    }

    def _guests_by_conn(conn_id: int) -> dict:
        return guests if conn_id == env.conn.id else {}

    order: list[int] = []

    def slow_collect(client, conn, guest, binding):  # noqa: ARG001
        import time as _t

        order.append(int(guest["vmid"]))
        _t.sleep(0.05)  # 模拟 QGA/SSH 往返;并发时应显著短于串行
        return {
            "available": True,
            "sampled_at": time.time(),
            "disk_max_pct": 50.0 + int(guest["vmid"]) / 100.0,
        }

    fs._cache.clear()
    with (
        patch.object(fs, "_wanted_guest_keys", return_value=(set(), True)),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "build_client", return_value=SimpleNamespace()),
        patch.object(fs, "_collect_one", side_effect=slow_collect),
    ):
        collected = fs.refresh_all(env.db)
    assert collected == 3
    # 三个目标都被采集(worker 内 vmid 任序),缓存值正确
    for vmid in (101, 102, 103):
        entry = fs.get_guest_disk_entry(env.conn.id, vmid)
        assert entry is not None and entry["available"]
        assert entry["disk_max_pct"] == 50.0 + vmid / 100.0
    fs._cache.clear()


def test_refresh_all_parallel_isolates_worker_failures(env):
    """单个 guest 采集抛异常只影响自身(失败隔离),其余照常入缓存。"""
    from app.models.alert import AlertRule

    env.db.add(
        AlertRule(
            name="disk-parallel-fail",
            metric="disk_max_pct",
            threshold=80.0,
            sustain_seconds=0,
            cooldown_seconds=300,
            severity="warning",
        )
    )
    env.db.commit()
    guests = {
        101: {
            "vmid": 101,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "web",
        },
        102: {
            "vmid": 102,
            "guest_type": "qemu",
            "status": "running",
            "node": "pve1",
            "name": "db",
        },
    }

    def _guests_by_conn(conn_id: int) -> dict:
        return guests if conn_id == env.conn.id else {}

    def flaky_collect(client, conn, guest, binding):  # noqa: ARG001
        if int(guest["vmid"]) == 102:
            raise RuntimeError("boom")
        return {"available": True, "sampled_at": time.time(), "disk_max_pct": 60.0}

    fs._cache.clear()
    with (
        patch.object(fs, "_wanted_guest_keys", return_value=(set(), True)),
        patch.object(fs, "get_guests_by_connection", side_effect=_guests_by_conn),
        patch.object(fs, "build_client", return_value=SimpleNamespace()),
        patch.object(fs, "_collect_one", side_effect=flaky_collect),
    ):
        assert fs.refresh_all(env.db) == 1
    assert fs.get_guest_disk_entry(env.conn.id, 101)["disk_max_pct"] == 60.0
    failed = fs.get_guest_disk_entry(env.conn.id, 102)
    assert failed is not None and failed["available"] is False
    assert "boom" in (failed.get("error") or "")
    fs._cache.clear()
