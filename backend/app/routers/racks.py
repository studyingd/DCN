from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.rack import RackCreate, RackResponse, RackUpdate
from app.services.permissions import require_permission
from app.utils import apply_update

router = APIRouter(tags=["racks"])


@router.get("/api/rooms/{room_id}/racks", response_model=list[RackResponse])
def list_racks(
    room_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:view")),
):
    """获取机房下所有机架（含设备数量）"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")
    racks = db.query(Rack).filter(Rack.room_id == room_id).order_by(Rack.id).all()
    rack_ids = [rack.id for rack in racks]
    counts = (
        dict(
            db.query(Device.rack_id, func.count(Device.id))
            .filter(Device.rack_id.in_(rack_ids))
            .group_by(Device.rack_id)
            .all()
        )
        if rack_ids
        else {}
    )
    result = []
    for rack in racks:
        resp = RackResponse.model_validate(rack)
        resp.device_count = counts.get(rack.id, 0)
        result.append(resp)
    return result


@router.post(
    "/api/rooms/{room_id}/racks",
    response_model=RackResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rack(
    room_id: int,
    body: RackCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """在机房中创建机架"""
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机房不存在")
    rack = Rack(
        room_id=room_id,
        name=body.name,
        type=body.type,
        capacity_u=24 if body.type == "cabinet" else None,
    )
    db.add(rack)
    db.commit()
    db.refresh(rack)
    resp = RackResponse.model_validate(rack)
    resp.device_count = 0
    return resp


@router.put("/api/racks/{rack_id}", response_model=RackResponse)
def update_rack(
    rack_id: int,
    body: RackUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """更新机架信息"""
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    update_data = body.model_dump(exclude_unset=True)
    # 仅在请求显式变更了机架类型时才回写 capacity_u;cabinet 归一为 24,
    # 其它类型清空。否则一次改名/挪位就会把 shelf 既有的容量抹掉。
    if "type" in update_data:
        target_type = update_data["type"]
        update_data["capacity_u"] = 24 if target_type == "cabinet" else None
    apply_update(
        rack,
        update_data,
        [
            "name",
            "type",
            "capacity_u",
        ],
    )
    db.commit()
    db.refresh(rack)
    device_count = (
        db.query(func.count(Device.id)).filter(Device.rack_id == rack.id).scalar() or 0
    )
    resp = RackResponse.model_validate(rack)
    resp.device_count = device_count
    return resp


@router.delete("/api/racks/{rack_id}")
def delete_rack(
    rack_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    """删除机架（级联删除设备）"""
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    db.delete(rack)
    db.commit()
    return {"message": "机架已删除"}
