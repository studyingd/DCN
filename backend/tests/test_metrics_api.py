"""服务器指标查询 API 测试(/api/metrics/*)。"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.device import Device
from app.models.device_metric_sample import DeviceMetricSample
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.routers.metrics import _trend_bucket_seconds
from app.services.auth import create_access_token

client = TestClient(app)


def _create_role_and_user(
    db: Session, permissions: list[str]
) -> tuple[Role, User, str]:
    role = Role(
        name=f"metrics_role_{datetime.now(timezone.utc).timestamp()}",
        permissions=json.dumps(permissions),
        device_scope="all",
    )
    db.add(role)
    db.flush()

    user = User(
        username=f"metricsuser_{datetime.now(timezone.utc).timestamp()}",
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


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _metrics_entry(device_id: int, **overrides) -> dict:
    entry = {
        "device_id": device_id,
        "available": True,
        "error": None,
        "fetched_at": "2026-08-11T01:00:00Z",
        "source": "ssh",
        "cpu_pct": 12.5,
        "mem_pct": 40.0,
        "mem_used_mb": 3200,
        "mem_total_mb": 8000,
        "disk_max_pct": 55.0,
        "disks": [
            {
                "mount": "/",
                "size_bytes": 100_000_000_000,
                "used_bytes": 55_000_000_000,
                "pct": 55.0,
            }
        ],
        "load1": 0.5,
        "load5": 0.4,
        "load15": 0.3,
        "uptime_sec": 86400,
        "net_rx_bps": 1024.0,
        "net_tx_bps": 2048.0,
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def setup_data():
    """两台服务器(一在线一离线)+ 权限用户;测试后清理。"""
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)

    role, user, token = _create_role_and_user(db, ["device:view"])

    room = Room(name="Metrics Room", location="Floor 1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="Rack-M", type="cabinet")
    db.add(rack)
    db.flush()

    online_srv = Device(
        rack_id=rack.id,
        name="srv-online",
        type="server",
        ip_address="10.1.0.1",
        os_system="Rocky Linux 9.4",
        status="online",
        # /api/metrics/devices 只展示「能真正采集到指标」的设备,必须带凭据
        remote_username="root",
        remote_password_enc="enc:test",
    )
    offline_host = Device(
        rack_id=rack.id,
        name="host-offline",
        type="host",
        ip_address="10.1.0.2",
        os_system="windows",
        status="offline",
        remote_username="Administrator",
        remote_password_enc="enc:test",
    )
    db.add_all([online_srv, offline_host])
    db.flush()

    # 历史样本:在线服务器 3 个点
    now = datetime.now(timezone.utc)
    samples = [
        DeviceMetricSample(
            device_id=online_srv.id,
            ts=(now - timedelta(minutes=m)).replace(tzinfo=None),
            cpu_pct=float(10 + m),
            mem_pct=40.0,
            disk_max_pct=55.0,
            load1=0.5,
            net_rx_bps=1000.0,
            net_tx_bps=2000.0,
        )
        for m in (2, 1, 0)
    ]
    db.add_all(samples)
    db.commit()

    statuses = {online_srv.id: "online", offline_host.id: "offline"}
    latest = {
        online_srv.id: _metrics_entry(online_srv.id),
        offline_host.id: _metrics_entry(
            offline_host.id,
            available=False,
            error="设备离线",
            fetched_at=None,
            source=None,
        ),
    }

    try:
        yield db, token, online_srv, offline_host, statuses, latest
    finally:
        db.query(DeviceMetricSample).filter(
            DeviceMetricSample.device_id.in_([online_srv.id, offline_host.id])
        ).delete()
        db.query(Device).filter(
            Device.id.in_([online_srv.id, offline_host.id])
        ).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


def test_list_metrics(setup_data):
    db, token, online_srv, offline_host, statuses, latest = setup_data
    with (
        patch("app.routers.metrics.get_latest_statuses", return_value=statuses),
        patch("app.routers.metrics.get_latest_metrics", return_value=latest),
    ):
        resp = client.get("/api/metrics/devices", headers=_auth_headers(token))

    assert resp.status_code == 200
    data = resp.json()
    # 测试库=开发库,列表里可能混有其它设备,只断言本测试创建的设备。
    # Windows 且最近一次采集失败(available=False)的设备按新规则从列表剔除——
    # 它对运维只有「WinRM 执行失败」噪声;offline_host 正是这个场景。
    items = {i["device_id"]: i for i in data["items"]}

    online = items[online_srv.id]
    assert online["available"] is True
    assert online["cpu_pct"] == 12.5
    assert online["status"] == "online"
    # 分机房/机柜查看所需的位置字段
    assert online["room_name"] == "Metrics Room"
    assert online["rack_name"] == "Rack-M"
    assert online["room_id"] is not None
    assert online["rack_id"] is not None

    assert offline_host.id not in items


def test_device_detail(setup_data):
    db, token, online_srv, _, statuses, latest = setup_data
    with (
        patch("app.routers.metrics.get_latest_statuses", return_value=statuses),
        patch(
            "app.routers.metrics.get_device_metrics", return_value=latest[online_srv.id]
        ),
    ):
        resp = client.get(
            f"/api/metrics/devices/{online_srv.id}", headers=_auth_headers(token)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["mem_used_mb"] == 3200
    assert data["load5"] == 0.4
    assert data["disks"][0]["mount"] == "/"


def test_history(setup_data):
    db, token, online_srv, _, _, _ = setup_data
    resp = client.get(
        f"/api/metrics/devices/{online_srv.id}/history",
        params={"range": "24h"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "24h"
    # 降采样在 SQL 里按绝对时间桶做(24h/500 点 → 172s 桶宽):三个相隔 1 分钟的
    # 样本可能落在 2~3 个桶,桶内均值。点数只做区间断言,不锁定跨桶边界。
    assert 1 <= len(data["points"]) <= 3
    # 按时间升序
    ts_list = [p["ts"] for p in data["points"]]
    assert ts_list == sorted(ts_list)
    cpu_values = [p["cpu_pct"] for p in data["points"]]
    assert all(v is not None and 10 <= v <= 13 for v in cpu_values)
    # 桶内均值不破坏取值范围:最早桶含 12(也可能与 11 合并成 11.5)
    assert min(cpu_values) <= 12.0


def test_history_invalid_range(setup_data):
    db, token, online_srv, _, _, _ = setup_data
    resp = client.get(
        f"/api/metrics/devices/{online_srv.id}/history",
        params={"range": "30d"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 400


def test_trend_aligns_series_timestamps(setup_data):
    """首页概览曲线:各服务器必须落在同一组时间桶上。

    同一轮采集里每台设备各自完成 SSH/WinRM 往返后才落库，时间戳相差数秒;而
    ECharts 的 axis tooltip 只保留 x 值离指针最近的那一条 series，时间戳不对齐
    就会退化成"同一时刻只显示一台服务器"。
    """
    db, token, online_srv, offline_host, _, _ = setup_data
    # 锚定到桶起点，桶边界确定且样本都落在最近一小时内
    bucket_sec = _trend_bucket_seconds(1)
    base = datetime.fromtimestamp(
        int(datetime.now(timezone.utc).timestamp()) // bucket_sec * bucket_sec
        - bucket_sec * 3,
        tz=timezone.utc,
    ).replace(tzinfo=None)
    db.query(DeviceMetricSample).filter(
        DeviceMetricSample.device_id.in_([online_srv.id, offline_host.id])
    ).delete()
    db.add_all(
        DeviceMetricSample(
            device_id=device.id,
            ts=base + timedelta(seconds=bucket_sec * step + offset),
            cpu_pct=10.0,
            mem_pct=40.0,
        )
        for device, offset in ((online_srv, 1), (offline_host, 4))
        for step in (0, 0.5, 1)
    )
    db.commit()

    resp = client.get(
        "/api/metrics/history", params={"range": "1h"}, headers=_auth_headers(token)
    )

    assert resp.status_code == 200
    series = {item["device_id"]: item for item in resp.json()["series"]}
    assert {online_srv.id, offline_host.id} <= set(series)
    ts_a = [p["ts"] for p in series[online_srv.id]["points"]]
    ts_b = [p["ts"] for p in series[offline_host.id]["points"]]
    assert ts_a == ts_b
    # 前两个采样同桶(桶内取均值)，第三个进下一个桶
    assert ts_a == [
        base.isoformat() + "Z",
        (base + timedelta(seconds=bucket_sec)).isoformat() + "Z",
    ]


def test_trend_7d_keeps_newest_samples(setup_data):
    """回归：7d 全量样本(>10000 行)时，最新的样本不能再被截掉。

    旧实现 ``ORDER BY ts ASC + LIMIT 10000`` 保留的是最旧的 1 万行，7d 区间
    (60s 采集 × 10080 行)会把最新 ~80 分钟截掉，曲线右端缺一段。
    """
    db, token, online_srv, _, _, _ = setup_data
    db.query(DeviceMetricSample).filter(
        DeviceMetricSample.device_id == online_srv.id
    ).delete()
    now = datetime.now(timezone.utc)
    db.bulk_insert_mappings(
        DeviceMetricSample,
        [
            {
                "device_id": online_srv.id,
                "ts": (now - timedelta(minutes=m)).replace(tzinfo=None),
                "cpu_pct": 10.0,
                "mem_pct": 40.0,
            }
            for m in range(10080)
        ],
    )
    db.commit()

    resp = client.get(
        "/api/metrics/history", params={"range": "7d"}, headers=_auth_headers(token)
    )
    assert resp.status_code == 200
    series = {item["device_id"]: item for item in resp.json()["series"]}
    points = series[online_srv.id]["points"]
    assert points  # 7d 桅化后必有数据
    bucket_sec = _trend_bucket_seconds(24 * 7)
    # 桶数上限：区间跨桶边界时最多 目标桶数+1 个(180+1)
    assert len(points) <= 24 * 7 * 3600 // bucket_sec + 1
    last_ts = datetime.fromisoformat(points[-1]["ts"].replace("Z", "+00:00"))
    # 最新的样本(now)必须落在最后一个桶里(距桶起点不超过一个桶宽)
    assert (now - last_ts).total_seconds() < bucket_sec


def test_metrics_requires_permission():
    """无 device:view 权限 → 403。"""
    db = SessionLocal()
    try:
        role, user, token = _create_role_and_user(db, [])
        db.commit()  # 让 get_current_user 的独立会话能查到该用户
        try:
            resp = client.get("/api/metrics/devices", headers=_auth_headers(token))
            assert resp.status_code == 403
        finally:
            db.query(User).filter(User.id == user.id).delete()
            db.query(Role).filter(Role.id == role.id).delete()
            db.commit()
    finally:
        db.close()


def test_collect_endpoint_triggers_collection():
    """POST /api/metrics/collect 触发一轮真实采集并返回统计(手动刷新用)。"""
    db = SessionLocal()
    try:
        role, user, token = _create_role_and_user(db, ["device:view"])
        db.commit()
        try:
            with patch(
                "app.routers.metrics.trigger_collection",
                return_value={
                    1: {"device_id": 1, "available": True},
                    2: {"device_id": 2, "available": False},
                },
            ) as trig:
                resp = client.post("/api/metrics/collect", headers=_auth_headers(token))
            assert resp.status_code == 200
            assert resp.json() == {"total": 2, "available": 1}
            trig.assert_awaited_once()
        finally:
            db.query(User).filter(User.id == user.id).delete()
            db.query(Role).filter(Role.id == role.id).delete()
            db.commit()
    finally:
        db.close()
