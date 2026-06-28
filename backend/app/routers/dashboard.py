"""Big-screen dashboard API endpoints."""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.connection import Connection
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.dashboard import (
    AuditEventItem,
    AuditTimelineResponse,
    ConnectionTopologyResponse,
    DeviceTypeDistributionResponse,
    DeviceTypeItem,
    LoginTrendItem,
    LoginTrendResponse,
    OverviewResponse,
    RoomSummaryItem,
    RoomSummaryResponse,
    TopologyEdge,
    TopologyNode,
)
from app.services.monitor import get_latest_statuses
from app.services.permissions import get_user_device_ids, require_permission

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Chinese labels for device types
_TYPE_LABELS: dict[str, str] = {
    "server": "服务器",
    "switch": "交换机",
    "router": "路由器",
    "firewall": "防火墙",
    "host": "主机",
}


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """Overview statistics for the dashboard, filtered by RBAC device scope."""
    # RBAC: filter device-scoped queries
    allowed_ids = get_user_device_ids(current_user, db)

    room_count = db.query(func.count(Room.id)).scalar() or 0

    rack_query = db.query(func.count(Rack.id))
    if allowed_ids is not None:
        rack_query = rack_query.filter(
            Rack.id.in_(
                db.query(Device.rack_id).filter(Device.id.in_(allowed_ids)).subquery()
            )
        )
    rack_count = rack_query.scalar() or 0

    device_query = db.query(func.count(Device.id))
    if allowed_ids is not None:
        device_query = device_query.filter(Device.id.in_(allowed_ids))
    device_total = device_query.scalar() or 0

    # Connections: only count those where both endpoints are accessible
    conn_query = db.query(func.count(Connection.id))
    if allowed_ids is not None:
        conn_query = conn_query.filter(
            Connection.device_a_id.in_(allowed_ids),
            Connection.device_b_id.in_(allowed_ids),
        )
    connection_count = conn_query.scalar() or 0
    user_count = db.query(func.count(User.id)).scalar() or 0

    # Get live device statuses from the monitor service, filtered by RBAC
    all_statuses = get_latest_statuses()
    if allowed_ids is not None:
        statuses = {k: v for k, v in all_statuses.items() if k in allowed_ids}
    else:
        statuses = all_statuses
    device_online = sum(1 for s in statuses.values() if s == "online")
    device_offline = sum(1 for s in statuses.values() if s == "offline")
    device_maintenance = sum(1 for s in statuses.values() if s == "maintenance")

    # Get online user count
    online_user_count = 0
    try:
        from app.services.online_users import get_online_users

        online_user_count = len(get_online_users())
    except Exception:
        pass

    return OverviewResponse(
        room_count=room_count,
        rack_count=rack_count,
        device_total=device_total,
        device_online=device_online,
        device_offline=device_offline,
        device_maintenance=device_maintenance,
        connection_count=connection_count,
        user_count=user_count,
        online_user_count=online_user_count,
    )


@router.get("/device-type-distribution", response_model=DeviceTypeDistributionResponse)
def get_device_type_distribution(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """Device count distribution by type with online/offline breakdown."""
    # Group devices by type
    type_counts = (
        db.query(Device.type, func.count(Device.id)).group_by(Device.type).all()
    )

    statuses = get_latest_statuses()

    # Build per-type status counts from the monitor cache
    # First, get device type mapping
    device_types: dict[int, str] = {}
    for row in db.query(Device.id, Device.type).all():
        device_types[row[0]] = row[1]

    type_status: dict[str, dict[str, int]] = {}
    for device_id, status_val in statuses.items():
        dtype = device_types.get(device_id)
        if dtype is None:
            continue
        if dtype not in type_status:
            type_status[dtype] = {"online": 0, "offline": 0, "maintenance": 0}
        if status_val in type_status[dtype]:
            type_status[dtype][status_val] += 1

    distribution = []
    for dtype, count in type_counts:
        ts = type_status.get(dtype, {"online": 0, "offline": 0, "maintenance": 0})
        distribution.append(
            DeviceTypeItem(
                type=dtype,
                label=_TYPE_LABELS.get(dtype, dtype),
                count=count,
                online=ts["online"],
                offline=ts["offline"],
                maintenance=ts["maintenance"],
            )
        )

    return DeviceTypeDistributionResponse(distribution=distribution)


@router.get("/room-summary", response_model=RoomSummaryResponse)
def get_room_summary(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """Per-room device statistics."""
    statuses = get_latest_statuses()

    # Build a mapping from rack_id -> room_id
    rack_room: dict[int, int] = {}
    for row in db.query(Rack.id, Rack.room_id).all():
        rack_room[row[0]] = row[1]

    # Build per-room device counts and status breakdowns
    # device_id -> rack_id
    device_rack: dict[int, int] = {}
    for row in db.query(Device.id, Device.rack_id).all():
        device_rack[row[0]] = row[1]

    # Aggregate: room_id -> {device_ids}
    room_devices: dict[int, set[int]] = {}
    for device_id, rack_id in device_rack.items():
        room_id = rack_room.get(rack_id)
        if room_id is not None:
            if room_id not in room_devices:
                room_devices[room_id] = set()
            room_devices[room_id].add(device_id)

    # Rack counts per room
    rack_counts = (
        db.query(Rack.room_id, func.count(Rack.id)).group_by(Rack.room_id).all()
    )
    rack_count_map: dict[int, int] = {row[0]: row[1] for row in rack_counts}

    rooms = db.query(Room).order_by(Room.id).all()
    items = []
    for room in rooms:
        dev_ids = room_devices.get(room.id, set())
        dev_count = len(dev_ids)
        online = sum(1 for did in dev_ids if statuses.get(did) == "online")
        offline = sum(1 for did in dev_ids if statuses.get(did) == "offline")
        maintenance = sum(1 for did in dev_ids if statuses.get(did) == "maintenance")

        items.append(
            RoomSummaryItem(
                id=room.id,
                name=room.name,
                location=room.location,
                rack_count=rack_count_map.get(room.id, 0),
                device_count=dev_count,
                device_online=online,
                device_offline=offline,
                device_maintenance=maintenance,
            )
        )

    return RoomSummaryResponse(rooms=items)


@router.get("/connection-topology", response_model=ConnectionTopologyResponse)
def get_connection_topology(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    """Device connection topology with RBAC filtering."""
    # RBAC: get allowed device IDs
    allowed_ids = get_user_device_ids(current_user, db)

    # Query devices (apply RBAC filter)
    device_query = db.query(Device)
    if allowed_ids is not None:
        device_query = device_query.filter(Device.id.in_(allowed_ids))
    devices = device_query.all()

    # Build a set of accessible device IDs
    accessible_ids = {d.id for d in devices}

    # Build rack_id -> room mapping
    rack_room_cache: dict[int, tuple[int, str]] = {}
    racks = db.query(Rack.id, Rack.room_id).all()
    room_ids = {r[1] for r in racks}
    room_map: dict[int, str] = {}
    if room_ids:
        for row in db.query(Room.id, Room.name).filter(Room.id.in_(room_ids)).all():
            room_map[row[0]] = row[1]
    for rack_id, room_id in racks:
        rack_room_cache[rack_id] = (room_id, room_map.get(room_id, ""))

    statuses = get_latest_statuses()

    # Build nodes
    nodes = []
    for d in devices:
        room_info = rack_room_cache.get(d.rack_id)
        room_id_val = room_info[0] if room_info else None
        room_name_val = room_info[1] if room_info else None

        nodes.append(
            TopologyNode(
                id=d.id,
                name=d.name,
                type=d.type,
                ip=d.ip_address,
                status=statuses.get(d.id, d.status or "offline"),
                room_id=room_id_val,
                room_name=room_name_val,
            )
        )

    # Query connections where both endpoints are accessible
    conn_query = db.query(Connection)
    if allowed_ids is not None:
        conn_query = conn_query.filter(
            Connection.device_a_id.in_(accessible_ids),
            Connection.device_b_id.in_(accessible_ids),
        )
    connections = conn_query.all()

    edges = []
    for c in connections:
        edges.append(
            TopologyEdge(
                source=c.device_a_id,
                target=c.device_b_id,
                conn_type=c.conn_type,
                bandwidth=c.bandwidth,
            )
        )

    return ConnectionTopologyResponse(nodes=nodes, edges=edges)


@router.get("/audit-timeline", response_model=AuditTimelineResponse)
def get_audit_timeline(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """Recent audit events and today's event type counts."""
    # Start of today (UTC)
    today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)

    # Event counts grouped by type for today
    event_counts_rows = (
        db.query(AuditLog.event_type, func.count(AuditLog.id))
        .filter(AuditLog.created_at >= today_start)
        .group_by(AuditLog.event_type)
        .all()
    )

    event_counts_today = {row[0]: row[1] for row in event_counts_rows}

    # Latest 20 audit events
    recent_logs = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(20).all()

    recent_events = []
    for log in recent_logs:
        created_str = log.created_at.isoformat() if log.created_at else ""
        recent_events.append(
            AuditEventItem(
                id=log.id,
                event_type=log.event_type,
                username=log.username,
                device_name=log.device_name,
                command=log.command,
                created_at=created_str,
            )
        )

    return AuditTimelineResponse(
        recent_events=recent_events,
        event_counts_today=event_counts_today,
    )


@router.get("/login-trend", response_model=LoginTrendResponse)
def get_login_trend(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """Daily login count trend over the past N days."""
    cutoff = datetime.combine(
        date.today() - timedelta(days=days - 1), time.min, tzinfo=timezone.utc
    )

    rows = (
        db.query(func.date(AuditLog.created_at), func.count(AuditLog.id))
        .filter(
            AuditLog.event_type == "system_login",
            AuditLog.created_at >= cutoff,
        )
        .group_by(func.date(AuditLog.created_at))
        .all()
    )

    # Build a lookup dict from query results
    count_map: dict[str, int] = {}
    for date_val, count in rows:
        # date_val may be a date or string depending on driver
        key = str(date_val)
        count_map[key] = count

    # Fill in all days in range, including days with zero logins
    trend = []
    for i in range(days):
        d = date.today() - timedelta(days=days - 1 - i)
        key = d.isoformat()
        trend.append(
            LoginTrendItem(
                date=key,
                login_count=count_map.get(key, 0),
            )
        )

    return LoginTrendResponse(trend=trend)
