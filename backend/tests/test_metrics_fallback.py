"""metrics_collector 读取侧的 DB 回退(多副本/冷启动场景)。

非 leader 实例不跑采集,进程内 ``_latest`` 永远是空的;采集循环没跑完第一轮
的冷启动进程同样为空。读取路径(get_latest_metrics / get_device_metrics)此时
应回退查 device_metric_samples 每台设备最新一行,而不是返回"无数据"。

覆盖:
- 空 _latest → 回退生效,DB 最新样本成为「最新值」;
- 新鲜 _latest(leader 进程)→ 不查库,内存值直接返回;
- _latest 全部超龄(本进程采集停摆)→ 回退补充;
- 超龄样本(采集停摆超过 METRICS_INTERVAL*2)→ 不算"有数据";
- _load_fallback 的 TTL 短缓存,10s 内不重复查库。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.device import Device
from app.models.device_metric_sample import DeviceMetricSample
from app.models.rack import Rack
from app.models.room import Room
from app.services import metrics_collector as mc


@pytest.fixture()
def sample_device():
    """一台带凭据的服务器 + 一条 5 秒前的新鲜样本;测试后清理。"""
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    room = Room(name="fb-room", location="t")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="fb-rack", type="cabinet")
    db.add(rack)
    db.flush()
    stamp = f"fb{datetime.now(timezone.utc).timestamp()}"
    device = Device(
        rack_id=rack.id,
        name=f"srv-{stamp}",
        type="server",
        ip_address="10.9.0.1",
        os_system="linux",
        status="online",
        remote_username="root",
        remote_password_enc="enc",
    )
    db.add(device)
    db.flush()
    sample = DeviceMetricSample(
        device_id=device.id,
        ts=datetime.now(timezone.utc) - timedelta(seconds=5),
        cpu_pct=17.0,
        mem_pct=42.0,
        mem_used_mb=4096,
        mem_total_mb=8192,
        disk_max_pct=55.0,
        disks_json='[{"mount": "/", "size_bytes": 100, "used_bytes": 55, "pct": 55.0}]',
        load1=0.1,
        load5=0.2,
        load15=0.3,
        uptime_sec=3600,
        net_rx_bps=1024.0,
        net_tx_bps=2048.0,
        disk_read_bps=8.0,
        disk_write_bps=16.0,
    )
    db.add(sample)
    db.commit()
    device_id = device.id
    sample_id = sample.id
    rack_id, room_id = rack.id, room.id
    yield device_id
    db.query(DeviceMetricSample).filter(DeviceMetricSample.id == sample_id).delete()
    db.query(Device).filter(Device.id == device_id).delete()
    db.query(Rack).filter(Rack.id == rack_id).delete()
    db.query(Room).filter(Room.id == room_id).delete()
    db.commit()
    db.close()


def _reset_state():
    mc._latest.clear()
    mc._fallback_cache.clear()
    mc._fallback_loaded_at = 0.0


def test_empty_latest_falls_back_to_db(sample_device):
    _reset_state()
    # 模拟非 leader 进程:_latest 从未被填充
    with patch.object(mc, "_fallback_from_db", wraps=mc._fallback_from_db) as spy:
        entry = mc.get_device_metrics(sample_device)
        assert entry is not None
        assert entry["available"] is True
        assert entry["cpu_pct"] == 17.0
        assert entry["mem_pct"] == 42.0
        assert entry["disks"][0]["mount"] == "/"
        assert spy.call_count == 1
        latest = mc.get_latest_metrics()
        assert latest[sample_device]["cpu_pct"] == 17.0
        # TTL 缓存:第二次读取不再查库
        assert spy.call_count == 1


def test_fresh_latest_skips_db(sample_device):
    _reset_state()
    fresh = {
        "device_id": sample_device,
        "available": True,
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "ssh",
        "cpu_pct": 99.0,
        "mem_pct": 1.0,
    }
    mc._latest[sample_device] = fresh
    with patch.object(mc, "_fallback_from_db", return_value={}) as spy:
        assert mc.get_device_metrics(sample_device)["cpu_pct"] == 99.0
        latest = mc.get_latest_metrics()
        assert latest[sample_device]["cpu_pct"] == 99.0
        assert spy.call_count == 0  # 内存新鲜,绝不查库


def test_stale_latest_uses_db(sample_device):
    _reset_state()
    stale = {
        "device_id": sample_device,
        "available": True,
        "error": None,
        # fetched_at 早于 _FALLBACK_MAX_AGE_SECONDS
        "fetched_at": (
            datetime.now(timezone.utc)
            - timedelta(seconds=mc._FALLBACK_MAX_AGE_SECONDS * 3)
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "ssh",
        "cpu_pct": 99.0,
        "mem_pct": 1.0,
    }
    mc._latest[sample_device] = stale
    entry = mc.get_device_metrics(sample_device)
    assert entry["cpu_pct"] == 17.0  # DB 样本盖过超龄内存值


def test_oversized_sample_not_treated_as_data(sample_device):
    """超龄样本不算"有数据"——但设备绑了凭据,应得到 available=False 合成条目。"""
    _reset_state()
    db: Session = SessionLocal()
    try:
        db.query(DeviceMetricSample).filter(
            DeviceMetricSample.device_id == sample_device
        ).update({"ts": datetime.now(timezone.utc) - timedelta(days=1)})
        db.commit()
    finally:
        db.close()
    entry = mc.get_device_metrics(sample_device)
    # 合成条目:明确说"最近无采集数据",列表页据此带原因显示或按规则剔除,
    # 而不是退回「还没采集过」的含糊兜底。
    assert entry is not None
    assert entry["available"] is False
    assert "采集" in str(entry["error"])


def test_uncredentialed_device_not_synthized(sample_device):
    """未绑凭据且无新鲜样本的设备不补合成条目(由列表页自己的凭据规则处理,
    不冒充「采集失败」)。设备有新鲜样本时仍返回 available=True——样本真实
    存在,与凭据无关。"""
    _reset_state()
    db: Session = SessionLocal()
    try:
        # 同时清凭据和样本(样本超龄 = 无新鲜数据)
        db.query(Device).filter(Device.id == sample_device).update(
            {"remote_username": None}
        )
        db.query(DeviceMetricSample).filter(
            DeviceMetricSample.device_id == sample_device
        ).update({"ts": datetime.now(timezone.utc) - timedelta(days=1)})
        db.commit()
    finally:
        db.close()
    assert mc.get_device_metrics(sample_device) is None


def test_list_endpoint_with_fallback(sample_device):
    """非 leader 进程经 API 路径读列表:_latest 空,DB 样本可见。"""
    import json

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.role import Role
    from app.models.user import User
    from app.services.auth import create_access_token

    # 裸 TestClient(不带 with):不触发 lifespan,否则会拉起后台采集器并真实
    # 采集开发库里的设备,context 退出时被硬掐还会留下悬空事务,把 conftest
    # janitor 的 DELETE 锁到超时(与 test_metrics_api.py 同一口径)。
    client = TestClient(app)

    db = SessionLocal()
    try:
        stamp = f"fb{datetime.now(timezone.utc).timestamp()}"
        role = Role(
            name=f"role-{stamp}",
            permissions=json.dumps(["device:view"]),
            device_scope="all",
        )
        db.add(role)
        db.flush()
        user = User(
            username=f"user-{stamp}",
            password="x",
            role="viewer",
            is_active=1,
            role_id=role.id,
        )
        db.add(user)
        db.commit()
        # id 在 session 关闭前取出,避免 DetachedInstanceError
        user_id, role_id = user.id, role.id
        token = create_access_token(
            user_id=user.id,
            username=user.username,
            role=user.role,
            permissions=["device:view"],
            device_scope="all",
        )
    finally:
        db.close()

    _reset_state()
    try:
        resp = client.get(
            "/api/metrics/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        item = next(
            (i for i in resp.json()["items"] if i["device_id"] == sample_device),
            None,
        )
        assert item is not None
        assert item["available"] is True
        assert item["cpu_pct"] == 17.0
    finally:
        db = SessionLocal()
        try:
            db.query(User).filter(User.id == user_id).delete()
            db.query(Role).filter(Role.id == role_id).delete()
            db.commit()
        finally:
            db.close()
