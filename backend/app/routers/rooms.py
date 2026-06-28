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
    result = []
    for room in rooms:
        rack_count = (
            db.query(func.count(Rack.id)).filter(Rack.room_id == room.id).scalar() or 0
        )
        room_dict = RoomResponse.model_validate(room)
        room_dict.rack_count = rack_count
        result.append(room_dict)
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
        floor_plan=body.floor_plan,
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

    from app.services.permissions import get_user_device_ids

    allowed_ids = get_user_device_ids(current_user, db)

    racks = db.query(Rack).filter(Rack.room_id == room_id).all()
    rack_list = []
    for r in racks:
        query = db.query(Device).filter(Device.rack_id == r.id)
        if allowed_ids is not None:
            query = query.filter(Device.id.in_(allowed_ids))
        devices = query.all()
        if not devices and allowed_ids is not None:
            continue
        rack_brief = RackBrief.model_validate(r)
        rack_brief.devices = [DeviceBrief.model_validate(d) for d in devices]
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
    apply_update(room, update_data, ["name", "location", "description", "floor_plan"])
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
