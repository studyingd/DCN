from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.device import DeviceBrief
from app.schemas.room import RackBrief, RoomCreate, RoomDetail, RoomResponse, RoomUpdate
from app.services.metrics_collector import get_latest_metrics
from app.services.permissions import get_user_device_ids, require_permission
from app.utils import apply_update

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


@router.get("/{room_id}/devices")
def list_room_devices(
    room_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """获取机房下所有设备（跨机架），按机架分组"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")

    allowed_ids = get_user_device_ids(current_user, db)
    racks = db.query(Rack).filter(Rack.room_id == room_id).order_by(Rack.id).all()
    # Windows 通道连通信号(与 /api/racks 设备列表同口径):DeviceSelector 以
    # 本接口为候选源,告警中心/自动化运维据此置灰 WinRM 确认不通的目标。
    metrics_latest = get_latest_metrics()
    result = []
    for rack in racks:
        devices = (
            db.query(Device)
            .filter(Device.rack_id == rack.id)
            .order_by(
                case((Device.position_u == None, 1), else_=0),
                Device.position_u,
                Device.id,
            )
            .all()
        )
        for d in devices:
            if allowed_ids is not None and d.id not in allowed_ids:
                continue
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "type": d.type,
                    "ip_address": d.ip_address,
                    "rack_id": rack.id,
                    "rack_name": rack.name,
                    "position_u": d.position_u,
                    "size_u": d.size_u,
                    "status": d.status,
                    "os_system": d.os_system,
                    "has_credential": d.has_credential,
                    "metrics_failed": bool(
                        d.is_windows
                        and metrics_latest.get(d.id, {}).get("available") is False
                    ),
                }
            )
    return result


@router.get("", response_model=list[RoomResponse])
def list_rooms(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """获取所有机房列表（含机架数量）"""
    rooms = db.query(Room).order_by(Room.id.desc()).all()
    counts = dict(
        db.query(Rack.room_id, func.count(Rack.id)).group_by(Rack.room_id).all()
    )
    result = []
    for room in rooms:
        room_dict = RoomResponse.model_validate(room)
        room_dict.rack_count = counts.get(room.id, 0)
        result.append(room_dict)
    return result


@router.get("/tree", response_model=list[RoomDetail])
def list_room_tree(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """一次返回机房、机架和设备树，消除前端按机房逐个请求的 N+1。"""
    allowed_ids = get_user_device_ids(current_user, db)
    rooms = db.query(Room).order_by(Room.id.desc()).all()
    racks = db.query(Rack).order_by(Rack.room_id, Rack.id).all()
    rack_ids = [rack.id for rack in racks]
    device_query = (
        db.query(Device).filter(Device.rack_id.in_(rack_ids)) if rack_ids else None
    )
    if device_query is not None and allowed_ids is not None:
        device_query = device_query.filter(Device.id.in_(allowed_ids))
    devices_by_rack: dict[int, list[Device]] = {}
    if device_query is not None:
        for device in device_query.order_by(
            Device.rack_id, Device.position_u, Device.id
        ).all():
            devices_by_rack.setdefault(device.rack_id, []).append(device)
    racks_by_room: dict[int, list[RackBrief]] = {}
    for rack in racks:
        devices = devices_by_rack.get(rack.id, [])
        if allowed_ids is not None and not devices:
            continue
        item = RackBrief.model_validate(rack)
        item.devices = [DeviceBrief.model_validate(device) for device in devices]
        racks_by_room.setdefault(rack.room_id, []).append(item)
    result: list[RoomDetail] = []
    for room in rooms:
        item = RoomDetail.model_validate(room)
        item.racks = racks_by_room.get(room.id, [])
        item.rack_count = len(item.racks)
        if allowed_ids is not None and not item.racks:
            # 受限用户：一台授权设备都没有的机房不返回(旧实现返回全部机房名，
            # 越权实测：单虚机授权账号拿到了全部机房拓扑)。
            continue
        result.append(item)
    return result


@router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    body: RoomCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """创建机房"""
    room = Room(
        name=body.name,
        location=body.location,
        description=body.description,
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    resp = RoomResponse.model_validate(room)
    resp.rack_count = 0
    return resp


@router.get("/{room_id}", response_model=RoomDetail)
def get_room(
    room_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """获取机房详情（含机架和设备列表）"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")

    allowed_ids = get_user_device_ids(current_user, db)

    racks = db.query(Rack).filter(Rack.room_id == room_id).all()
    rack_ids = [rack.id for rack in racks]
    device_query = (
        db.query(Device).filter(Device.rack_id.in_(rack_ids)) if rack_ids else None
    )
    if device_query is not None and allowed_ids is not None:
        device_query = device_query.filter(Device.id.in_(allowed_ids))
    devices_by_rack: dict[int, list[Device]] = {}
    if device_query is not None:
        for device in device_query.all():
            devices_by_rack.setdefault(device.rack_id, []).append(device)

    rack_list = []
    for rack in racks:
        devices = devices_by_rack.get(rack.id, [])
        if not devices and allowed_ids is not None:
            continue
        rack_brief = RackBrief.model_validate(rack)
        rack_brief.devices = [DeviceBrief.model_validate(device) for device in devices]
        rack_list.append(rack_brief)
    resp = RoomDetail.model_validate(room)
    resp.rack_count = len(rack_list)
    resp.racks = rack_list
    return resp


@router.put("/{room_id}", response_model=RoomResponse)
def update_room(
    room_id: int,
    body: RoomUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """更新机房信息"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")
    update_data = body.model_dump(exclude_unset=True)
    apply_update(room, update_data, ["name", "location", "description"])
    db.commit()
    db.refresh(room)
    rack_count = (
        db.query(func.count(Rack.id)).filter(Rack.room_id == room.id).scalar() or 0
    )
    resp = RoomResponse.model_validate(room)
    resp.rack_count = rack_count
    return resp


@router.delete("/{room_id}")
def delete_room(
    room_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """删除机房（级联删除机架和设备）"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")
    db.delete(room)
    db.commit()
    return {"message": "机房已删除"}
