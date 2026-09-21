"""Server metrics query API — list / detail / history.

数据源:
  * 最新值:metrics_collector 的内存缓存(秒级读取,无 DB 压力);
  * 历史:device_metric_samples 表,>500 点时按时间桶平均降采样。
"""

import time as _time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import METRICS_INTERVAL
from app.database import get_db
from app.models.device import OPS_TARGET_TYPES, Device, is_windows_os
from app.models.device_metric_sample import DeviceMetricSample
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.metrics import (
    DeviceMetricsDetail,
    DeviceMetricsListResponse,
    DeviceMetricsSummary,
    DeviceMetricTrend,
    DiskUsage,
    MetricSamplePoint,
    MetricsHistoryResponse,
    MetricsTrendResponse,
    MetricTrendPoint,
)
from app.services.metrics_collector import (
    get_device_metrics,
    get_latest_metrics,
    trigger_collection,
)
from app.services.monitor import get_latest_statuses
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

_RANGES = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7}
_MAX_POINTS = 500
# 首页概览一次返回全部服务器曲线，每台最多 180 个时间桶，控制载荷体积。
_TREND_MAX_POINTS = 180


def _summary(
    device,
    m: dict,
    statuses: dict[int, str],
    rack_map: dict[int, tuple[str, int | None, str]],
) -> DeviceMetricsSummary:
    """摘要组装:device 可为全列 ORM,也可为列裁剪查询的具名 Row
    (两者属性名一致;列表页走后者,不再拉凭据密文等无用大列)。"""
    rack_name, room_id, room_name = rack_map.get(device.rack_id, ("", None, ""))
    return DeviceMetricsSummary(
        device_id=device.id,
        device_name=device.name,
        ip_address=device.ip_address,
        type=device.type,
        os_system=device.os_system,
        rack_id=device.rack_id,
        rack_name=rack_name,
        room_id=room_id,
        room_name=room_name,
        status=statuses.get(device.id, device.status or "offline"),
        available=bool(m.get("available")),
        error=m.get("error"),
        fetched_at=m.get("fetched_at"),
        source=m.get("source"),
        cpu_pct=m.get("cpu_pct"),
        mem_pct=m.get("mem_pct"),
        disk_max_pct=m.get("disk_max_pct"),
        load1=m.get("load1"),
        uptime_sec=m.get("uptime_sec"),
        net_rx_bps=m.get("net_rx_bps"),
        net_tx_bps=m.get("net_tx_bps"),
        disk_read_bps=m.get("disk_read_bps"),
        disk_write_bps=m.get("disk_write_bps"),
    )


def _get_server_or_404(device_id: int, db: Session, user: User) -> Device:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(user, device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    return device


@router.post("/collect")
async def trigger_metrics_collection(
    current_user: User = Depends(require_permission("device:view")),
):
    """手动触发一轮真实采集(监控页刷新按钮用)。

    与 monitor/scan 同口径:权限 device:view,复用 collector 内部的
    _collect_lock 与后台周期串行,不会并发双采;耗时可达一个采集周期
    (SSH/WinRM 串并发),调用方需容忍较长响应。采集是全量动作,不做
    RBAC 收窄(与后台周期同一份数据,读取侧照常过滤)。
    """
    latest = await trigger_collection()
    return {
        "total": len(latest),
        "available": sum(1 for m in latest.values() if m.get("available")),
    }


@router.get("/devices", response_model=DeviceMetricsListResponse)
def list_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """全部服务器的最新指标(RBAC 过滤),供监控列表页轮询。

    列裁剪:只取摘要与凭据存在性判断所需的列——列表页 30s 一轮轮询,
    拉全列 ORM 会把凭据密文等 Text 大列一并搬出来,而这里只做 bool 判断。
    """
    query = (
        db.query(
            Device.id,
            Device.name,
            Device.ip_address,
            Device.type,
            Device.os_system,
            Device.rack_id,
            Device.status,
            Device.remote_username,
            Device.remote_password_enc,
            Device.remote_ssh_key_enc,
        )
        .filter(Device.type.in_(OPS_TARGET_TYPES))
        .order_by(Device.id)
    )
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    devices = query.all()

    # 服务器监控只展示「能真正采集到指标」的设备：
    #   - Windows: 走 WinRM,必须有 username + password(端口默认 5985,谈不上"没配置"),
    #     且最近一次采集没失败(采集失败说明 WinRM 实际不可用,即使凭据填了也白搭)
    #   - Linux:   走 SSH,必须有 username + (password 或 ssh_key)
    # 没绑凭据 / 采集持续失败的设备永远只会显示「未绑定有效凭据」或「WinRM 执行失败」,
    # 对运维没有信息量,还会淹没列表,这里直接从列表里剔除。
    # 一旦 WinRM 修好、下一轮采集成功(最多 60s),设备会自动恢复显示,无需手工干预。
    # 设备详情页不受影响(用户仍可查看该设备的报错原因)。
    latest = get_latest_metrics()

    def _is_displayable(d) -> bool:
        username = (getattr(d, "remote_username", None) or "").strip()
        has_password = bool(getattr(d, "remote_password_enc", None))
        has_key = bool(getattr(d, "remote_ssh_key_enc", None))
        if not username:
            return False
        if is_windows_os(d.os_system):
            if not has_password:
                return False
            # 最近一次采集明确失败 → WinRM 实际不可用,隐藏
            # 还没采集过(available 键不存在) → 给一次机会,显示
            m = latest.get(d.id)
            if m is not None and m.get("available") is False:
                return False
            return True
        return has_password or has_key

    devices = [d for d in devices if _is_displayable(d)]

    rack_map = _rack_map_cached(db, {d.rack_id for d in devices})

    statuses = get_latest_statuses()

    items = [_summary(d, latest.get(d.id, {}), statuses, rack_map) for d in devices]
    return DeviceMetricsListResponse(items=items, total=len(items))


# rack/room 元数据 60s TTL:名称与归属变更频率远低于采集周期,列表页 10s 轮询
# 不必每轮都重查 rooms+racks 两张表;设备 CRUD 高频时上限也就是 60s 后生效。
_rack_map_cache: dict = {}
_RACK_MAP_TTL_SECONDS = 60.0


def _rack_map_cached(
    db: Session, rack_ids: set
) -> dict[int, tuple[str, int | None, str]]:
    """rack_id -> (rack_name, room_id, room_name),带 60s 进程内缓存。"""
    key = ("rack_map",)
    now = _time.monotonic()
    cached = _rack_map_cache.get(key)
    if cached and now - cached[0] < _RACK_MAP_TTL_SECONDS:
        return {rid: v for rid, v in cached[1].items() if rid in rack_ids}
    room_names = {r[0]: r[1] for r in db.query(Room.id, Room.name).all()}
    rows = db.query(Rack.id, Rack.name, Rack.room_id).all()
    full = {
        rack_id: (rack_name, room_id, room_names.get(room_id, ""))
        for rack_id, rack_name, room_id in rows
    }
    _rack_map_cache[key] = (now, full)
    return {rid: v for rid, v in full.items() if rid in rack_ids}


def reset_rack_map_cache() -> None:
    """清空 rack/room 元数据缓存(测试用)。"""
    _rack_map_cache.clear()


@router.get("/devices/{device_id}", response_model=DeviceMetricsDetail)
def device_metrics(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """单台服务器最新指标全量 + 分区明细。"""
    device = _get_server_or_404(device_id, db, current_user)
    m = get_device_metrics(device_id) or {}
    base = _summary(device, m, get_latest_statuses(), {})
    disks = [DiskUsage(**d) for d in m.get("disks", []) if isinstance(d, dict)]
    return DeviceMetricsDetail(
        **base.model_dump(),
        mem_used_mb=m.get("mem_used_mb"),
        mem_total_mb=m.get("mem_total_mb"),
        load5=m.get("load5"),
        load15=m.get("load15"),
        disks=disks,
    )


def _iso(ts: datetime) -> str:
    return ts.isoformat() + "Z" if ts.tzinfo is None else ts.isoformat()


def _trend_bucket_seconds(hours: int) -> int:
    """概览曲线的时间桶宽度(秒)——全部服务器共用同一组桶起点。

    采集时每台设备各自完成 SSH/WinRM 往返后才落库，同一轮里各台的时间戳相差
    数秒;而 ECharts 的 axis tooltip 只保留 x 值离指针最近的那一条 series，时间
    戳不对齐就表现为"同一时刻只显示一台服务器"。桶宽不小于采集周期的两倍，
    保证同一轮采样落进同一个桶，各 series 因此共享同一组时间戳。
    """
    return max(METRICS_INTERVAL * 2, -(-hours * 3600 // _TREND_MAX_POINTS))


@router.get("/devices/{device_id}/history", response_model=MetricsHistoryResponse)
def metrics_history(
    device_id: int,
    range: str = Query("1h"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """历史曲线。range: 1h/6h/24h/7d;超过 _MAX_POINTS 按时间桶平均降采样。

    降采样在 SQL 里做(GROUP BY 时间桶 + AVG),长区间不再把上万行原始样本
    拉进 Python;1h 以内行数本来就少,同样路径无害。
    """
    _get_server_or_404(device_id, db, current_user)
    hours = _RANGES.get(range)
    if hours is None:
        raise HTTPException(status_code=400, detail="range 必须是 1h/6h/24h/7d")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _bucketed_rows(
        db,
        device_id=device_id,
        since=since,
        hours=hours,
        columns=(
            DeviceMetricSample.cpu_pct,
            DeviceMetricSample.mem_pct,
            DeviceMetricSample.disk_max_pct,
            DeviceMetricSample.load1,
            DeviceMetricSample.net_rx_bps,
            DeviceMetricSample.net_tx_bps,
            DeviceMetricSample.disk_read_bps,
            DeviceMetricSample.disk_write_bps,
        ),
    )
    points = [MetricSamplePoint(ts=_iso(r[0]), **_avg_row(r)) for r in rows]
    return MetricsHistoryResponse(device_id=device_id, range=range, points=points)


def _bucketed_rows(
    db: Session,
    *,
    device_id: int,
    since: datetime,
    hours: int,
    columns: tuple,
) -> list:
    """按 ``floor(ts/桶宽)`` 分组聚合,返回 [(桶起点, 各列均值...)] 升序。

    桶宽按区间长度/目标点数向上取整,与旧 Python 桶宽语义一致。
    整除用 ``DIV`` 而非 ``//``(SQLAlchemy 编译成 ``//`` 时部分 MySQL 8
    小版本直接语法报错,DIV 是官方整数除操作符)。
    """
    bucket_sec = max(1, -(-hours * 3600 // _MAX_POINTS))
    bucket_start = func.from_unixtime(
        func.floor(func.unix_timestamp(DeviceMetricSample.ts) / bucket_sec) * bucket_sec
    )
    selected = [bucket_start.label("ts")]
    selected += [func.avg(c).label(c.key) for c in columns]
    rows = (
        db.query(*selected)
        .filter(
            DeviceMetricSample.device_id == device_id,
            DeviceMetricSample.ts >= since,
        )
        .group_by(bucket_start)
        .order_by(bucket_start)
        .all()
    )
    # SQLite 测试库/极旧 MySQL 返回字符串时归一为 datetime
    out = []
    for r in rows:
        ts = r[0]
        if not isinstance(ts, datetime):
            ts = datetime.fromtimestamp(
                int(float(ts)) * bucket_sec if ts is not None else 0
            )
        out.append((ts, *r[1:]))
    return out


def _bucketed_trend_rows(
    db: Session,
    *,
    device_ids: list[int],
    since: datetime,
    bucket_sec: int,
) -> list[tuple[int, datetime | None, float | None, float | None]]:
    """概览曲线的 SQL 桶化:全部设备一条查询取回 ``(device_id, 桶起点, cpu/mem 均值)``。

    与 ``_bucketed_rows`` 同一模式(FROM_UNIXTIME/FLOOR;engine 已把会话时区固定
    为 +00:00,桶起点即 UTC 墙钟,与 ``_iso`` 的 "Z" 约定一致),只是多一层
    device_id 分组:聚合完全在数据库侧完成,不再把原始样本逐台搬进 Python。
    """
    bucket_start = func.from_unixtime(
        func.floor(func.unix_timestamp(DeviceMetricSample.ts) / bucket_sec) * bucket_sec
    )
    rows = (
        db.query(
            DeviceMetricSample.device_id,
            bucket_start.label("ts"),
            func.avg(DeviceMetricSample.cpu_pct).label("cpu_pct"),
            func.avg(DeviceMetricSample.mem_pct).label("mem_pct"),
        )
        .filter(
            DeviceMetricSample.device_id.in_(device_ids),
            DeviceMetricSample.ts >= since,
        )
        .group_by(DeviceMetricSample.device_id, bucket_start)
        .order_by(DeviceMetricSample.device_id, bucket_start)
        .all()
    )
    out: list[tuple[int, datetime | None, float | None, float | None]] = []
    for device_id, ts, cpu_pct, mem_pct in rows:
        if ts is not None and not isinstance(ts, datetime):
            # 极旧驱动/方言返回字符串时按桶起点归一(与 _bucketed_rows 同款兜底)
            try:
                ts = datetime.fromtimestamp(int(float(ts)) * bucket_sec)
            except (TypeError, ValueError):
                ts = None
        out.append((int(device_id), ts, cpu_pct, mem_pct))
    return out


def _avg_row(row: tuple) -> dict:
    keys = (
        "cpu_pct",
        "mem_pct",
        "disk_max_pct",
        "load1",
        "net_rx_bps",
        "net_tx_bps",
        "disk_read_bps",
        "disk_write_bps",
    )
    return {
        k: (round(float(v), 2) if v is not None else None)
        for k, v in zip(keys, row[1:], strict=True)
    }


@router.get("/history", response_model=MetricsTrendResponse)
def metrics_trend(
    range: str = Query("1h"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """全部服务器的 CPU / 内存历史曲线，一次请求返回(首页概览图使用)。

    单条 SQL 按 ``(device_id, 绝对时间桶)`` 分组聚合一次取回,避免首页为每台
    服务器各发一次历史请求;聚合在数据库侧完成,不再把原始样本搬进 Python
    (旧实现 7d 区间逐台最多拉 1 万行,且 ``ORDER BY ts ASC + LIMIT`` 保留的
    是最旧行,会把 7d(10080 行)最新的 ~80 分钟截掉)。桶起点对齐语义不变
    (见 _trend_bucket_seconds)。设备范围同样受 RBAC 限制。
    """
    hours = _RANGES.get(range)
    if hours is None:
        raise HTTPException(status_code=400, detail="range 必须是 1h/6h/24h/7d")

    query = (
        db.query(Device).filter(Device.type.in_(OPS_TARGET_TYPES)).order_by(Device.id)
    )
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    devices = query.all()
    if not devices:
        return MetricsTrendResponse(range=range, total=0, series=[])

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket_sec = _trend_bucket_seconds(hours)
    rows = _bucketed_trend_rows(
        db,
        device_ids=[d.id for d in devices],
        since=since,
        bucket_sec=bucket_sec,
    )
    points_by_device: dict[int, list[MetricTrendPoint]] = {}
    for device_id, ts, cpu_pct, mem_pct in rows:
        if ts is None:
            continue
        points_by_device.setdefault(device_id, []).append(
            MetricTrendPoint(
                ts=_iso(ts),
                cpu_pct=round(float(cpu_pct), 2) if cpu_pct is not None else None,
                mem_pct=round(float(mem_pct), 2) if mem_pct is not None else None,
            )
        )
    series = [
        DeviceMetricTrend(
            device_id=device.id,
            device_name=device.name,
            points=points_by_device.get(device.id, []),
        )
        for device in devices
    ]
    return MetricsTrendResponse(range=range, total=len(series), series=series)
