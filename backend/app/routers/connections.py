from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.connection import Connection
from app.models.device import Device
from app.models.user import User
from app.schemas.connection import ConnectionCreate, ConnectionResponse
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)

router = APIRouter(prefix="/api/connections", tags=["connections"])


@router.get("", response_model=list[ConnectionResponse])
def list_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    # Eager-load both devices to avoid an N+1 (two Device queries per connection).
    connections = (
        db.query(Connection)
        .options(joinedload(Connection.device_a), joinedload(Connection.device_b))
        .order_by(Connection.id)
        .all()
    )
    allowed_ids = get_user_device_ids(current_user, db)
    result = []
    for conn in connections:
        if allowed_ids is not None:
            if (
                conn.device_a_id not in allowed_ids
                and conn.device_b_id not in allowed_ids
            ):
                continue
        resp = ConnectionResponse.model_validate(conn)
        resp.device_a_name = conn.device_a.name if conn.device_a else None
        resp.device_b_name = conn.device_b.name if conn.device_b else None
        result.append(resp)
    return result


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    if not user_can_access_device(current_user, body.device_a_id, db):
        raise HTTPException(403, detail="无权访问设备 A")
    if not user_can_access_device(current_user, body.device_b_id, db):
        raise HTTPException(403, detail="无权访问设备 B")
    device_a = db.query(Device).filter(Device.id == body.device_a_id).first()
    if not device_a:
        raise HTTPException(404, detail=f"设备 A (id={body.device_a_id}) 不存在")
    device_b = db.query(Device).filter(Device.id == body.device_b_id).first()
    if not device_b:
        raise HTTPException(404, detail=f"设备 B (id={body.device_b_id}) 不存在")
    if body.device_a_id == body.device_b_id:
        raise HTTPException(400, detail="不能连接设备到自身")

    # Normalize pair order (min, max) so A↔B collapse to one row and the
    # UNIQUE(device_a_id, device_b_id) constraint catches duplicates/retries.
    low_id, high_id = sorted((body.device_a_id, body.device_b_id))
    conn = Connection(
        device_a_id=low_id,
        device_b_id=high_id,
        conn_type=body.conn_type,
        bandwidth=body.bandwidth,
        note=body.note,
    )
    db.add(conn)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, detail="该设备对之间的连接已存在")
    db.refresh(conn)
    resp = ConnectionResponse.model_validate(conn)
    resp.device_a_name = device_a.name
    resp.device_b_name = device_b.name
    return resp


@router.delete("/{connection_id}")
def delete_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, detail="连接不存在")
    if not user_can_access_device(current_user, conn.device_a_id, db):
        raise HTTPException(403, detail="无权操作")
    db.delete(conn)
    db.commit()
    return {"message": "连接已删除"}
