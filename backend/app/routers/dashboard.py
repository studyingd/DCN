"""Big-screen dashboard API endpoints."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.alert import AlertEvent
from app.models.device import Device
from app.models.pve_connection import PveConnection
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.dashboard import (
    DeviceTypeDistributionResponse,
    DeviceTypeItem,
    OverviewResponse,
    RoomSummaryItem,
    RoomSummaryResponse,
)
from app.services.monitor import get_latest_statuses
from app.services.online_users import get_online_users
from app.services.permissions import (
    get_user_device_ids,
    get_user_pve_guest_keys,
    require_permission,
    user_can_access_pve,
    user_has_permission,
)
from app.services.pve_guest_status import get_guests_by_connection

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _accessible_guest_count(allowed, conn, guests: list[dict]) -> tuple[int, int]:
    """按角色虚拟机 ACL 统计 (总数, 运行中)，device_scope='all' 时不过滤。

    ``allowed`` 由调用方在本次请求里取一次(避免每个连接重查一次 ACL 表)。
    """
    if allowed is not None:
        guests = [
            guest
            for guest in guests
            if (
                conn.id,
                str(guest.get("guest_type") or guest.get("type") or "qemu"),
                _as_vmid(guest.get("vmid")),
            )
            in allowed
        ]
    running = sum(1 for guest in guests if str(guest.get("status")) == "running")
    return len(guests), running


def _as_vmid(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Chinese labels for device types
_TYPE_LABELS: dict[str, str] = {
    "server": "服务器",
    "cloud_server": "云服务器",
    "host": "台式主机",
}


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """Overview statistics for the dashboard, filtered by RBAC device scope."""
    # RBAC: filter device-scoped queries
    allowed_ids = get_user_device_ids(current_user, db)

    room_query = db.query(func.count(Room.id))
    if allowed_ids is not None:
        # 受限用户只统计有授权设备的机房(与 rack_count 同口径)，
        # 否则全局机房数泄露给单设备授权账号(越权实测)。
        room_query = room_query.filter(
            Room.id.in_(
                db.query(Rack.room_id)
                .filter(
                    Rack.id.in_(
                        db.query(Device.rack_id)
                        .filter(Device.id.in_(allowed_ids))
                        .subquery()
                    )
                )
                .subquery()
            )
        )
    room_count = room_query.scalar() or 0

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

    # user_count 属用户管理域信息，仅 user:manage 可见；
    # 其它角色拿到 0，避免受限账号枚举平台用户规模。
    user_count = (
        db.query(func.count(User.id)).scalar() or 0
        if user_has_permission(current_user, "user:manage", db)
        else 0
    )

    # Get live device statuses from the monitor service, filtered by RBAC
    all_statuses = get_latest_statuses()
    if allowed_ids is not None:
        statuses = {k: v for k, v in all_statuses.items() if k in allowed_ids}
    else:
        statuses = all_statuses
    device_online = sum(1 for s in statuses.values() if s == "online")
    device_offline = max(0, device_total - device_online)
    device_maintenance = 0

    pve_guest_total = pve_guest_running = pve_guest_stopped = 0
    if user_can_access_pve(current_user, db):
        # 读进程内快照(后台循环周期性拉取),请求路径不再阻塞在 PVE API 上。
        allowed_guest_keys = get_user_pve_guest_keys(current_user, db)
        for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
            guests = list(get_guests_by_connection(conn.id).values())
            total, running = _accessible_guest_count(allowed_guest_keys, conn, guests)
            pve_guest_total += total
            pve_guest_running += running
    pve_guest_stopped = max(0, pve_guest_total - pve_guest_running)
    pve_platform_count = (
        db.query(func.count(PveConnection.id))
        .filter(PveConnection.enabled == 1)
        .scalar()
        or 0
        if user_can_access_pve(current_user, db)
        else 0
    )
    device_total += pve_guest_total
    device_online += pve_guest_running
    device_offline += pve_guest_stopped

    # Get online user count
    try:
        online_user_count = len(get_online_users())
    except Exception:
        online_user_count = 0

    # 近 7 天告警事件数；与告警中心同一 RBAC 口径，受限用户只统计自己设备范围内的告警
    alert_since = datetime.now(timezone.utc) - timedelta(days=7)
    alert_query = db.query(func.count(AlertEvent.id)).filter(
        AlertEvent.first_triggered_at >= alert_since
    )
    if allowed_ids is not None:
        alert_query = alert_query.filter(AlertEvent.device_id.in_(allowed_ids))
    alert_events_7d = alert_query.scalar() or 0

    return OverviewResponse(
        room_count=room_count,
        rack_count=rack_count,
        device_total=device_total,
        device_online=device_online,
        device_offline=device_offline,
        device_maintenance=device_maintenance,
        user_count=user_count,
        online_user_count=online_user_count,
        pve_guest_total=pve_guest_total,
        pve_guest_running=pve_guest_running,
        pve_guest_stopped=pve_guest_stopped,
        pve_platform_count=pve_platform_count,
        alert_events_7d=alert_events_7d,
    )


@router.get("/device-type-distribution", response_model=DeviceTypeDistributionResponse)
def get_device_type_distribution(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """Device count distribution by type with online/offline breakdown."""
    allowed_ids = get_user_device_ids(current_user, db)
    # Group devices by type
    type_query = db.query(Device.type, func.count(Device.id))
    if allowed_ids is not None:
        type_query = type_query.filter(Device.id.in_(allowed_ids))
    type_counts = type_query.group_by(Device.type).all()

    statuses = get_latest_statuses()

    # Build per-type status counts from the monitor cache
    # First, get device type mapping
    device_types: dict[int, str] = {}
    device_type_query = db.query(Device.id, Device.type)
    if allowed_ids is not None:
        device_type_query = device_type_query.filter(Device.id.in_(allowed_ids))
    for row in device_type_query.all():
        device_types[row[0]] = row[1]

    type_status: dict[str, dict[str, int]] = {}
    for device_id, status_val in statuses.items():
        dtype = device_types.get(device_id)
        if dtype is None:
            continue
        if dtype not in type_status:
            type_status[dtype] = {"online": 0}
        if status_val == "online":
            type_status[dtype]["online"] += 1

    distribution = []
    for dtype, count in type_counts:
        ts = type_status.get(dtype, {"online": 0})
        distribution.append(
            DeviceTypeItem(
                type=dtype,
                label=_TYPE_LABELS.get(dtype, dtype),
                count=count,
                online=ts["online"],
                offline=max(0, count - ts["online"]),
                maintenance=0,
            )
        )

    # Include PVE guests in the same resource-type distribution as managed devices.
    pve_total = pve_running = 0
    if user_can_access_pve(current_user, db):
        allowed_guest_keys = get_user_pve_guest_keys(current_user, db)
        for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
            guests = list(get_guests_by_connection(conn.id).values())
            total, running = _accessible_guest_count(allowed_guest_keys, conn, guests)
            pve_total += total
            pve_running += running
    if pve_total > 0:
        distribution.append(
            DeviceTypeItem(
                type="vm",
                label="虚拟机",
                count=pve_total,
                online=pve_running,
                offline=pve_total - pve_running,
                maintenance=0,
            )
        )

    return DeviceTypeDistributionResponse(distribution=distribution)


@router.get("/room-summary", response_model=RoomSummaryResponse)
def get_room_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """Per-room device statistics."""
    allowed_ids = get_user_device_ids(current_user, db)
    statuses = get_latest_statuses()

    # Build a mapping from rack_id -> room_id
    rack_room: dict[int, int] = {}
    for row in db.query(Rack.id, Rack.room_id).all():
        rack_room[row[0]] = row[1]

    # Build per-room device counts and status breakdowns
    # device_id -> rack_id
    device_rack: dict[int, int] = {}
    device_rack_query = db.query(Device.id, Device.rack_id)
    if allowed_ids is not None:
        device_rack_query = device_rack_query.filter(Device.id.in_(allowed_ids))
    for row in device_rack_query.all():
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
    rack_query = db.query(Rack.room_id, func.count(Rack.id))
    if allowed_ids is not None:
        rack_query = rack_query.filter(
            Rack.id.in_(
                db.query(Device.rack_id).filter(Device.id.in_(allowed_ids)).subquery()
            )
        )
    rack_counts = rack_query.group_by(Rack.room_id).all()
    rack_count_map: dict[int, int] = {row[0]: row[1] for row in rack_counts}

    rooms = db.query(Room).order_by(Room.id).all()
    items = []
    for room in rooms:
        dev_ids = room_devices.get(room.id, set())
        if allowed_ids is not None and not dev_ids:
            # 受限用户：没有授权设备的机房不进统计列表(与 /rooms/tree 同口径)
            continue
        dev_count = len(dev_ids)
        online = sum(1 for did in dev_ids if statuses.get(did) == "online")
        offline = max(0, dev_count - online)

        items.append(
            RoomSummaryItem(
                id=room.id,
                name=room.name,
                location=room.location,
                rack_count=rack_count_map.get(room.id, 0),
                device_count=dev_count,
                device_online=online,
                device_offline=offline,
                device_maintenance=0,
            )
        )

    return RoomSummaryResponse(rooms=items)
