from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.rack import Rack
from app.models.user import User
from app.services import interface_status
from app.services.monitor import get_latest_statuses, trigger_scan
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


@router.get("/stats")
def get_device_stats(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    """Return total/online/offline device counts for the dashboard."""
    allowed_ids = get_user_device_ids(current_user, db)

    if allowed_ids is not None:
        total = len(allowed_ids)
    else:
        total = db.query(func.count(Device.id)).scalar() or 0

    statuses = get_latest_statuses()
    if allowed_ids is not None:
        statuses = {k: v for k, v in statuses.items() if k in allowed_ids}

    online = sum(1 for s in statuses.values() if s == "online")
    offline = sum(1 for s in statuses.values() if s == "offline")
    maintenance = sum(1 for s in statuses.values() if s == "maintenance")

    return {
        "total": total,
        "online": online,
        "offline": offline,
        "maintenance": maintenance,
    }


@router.get("/statuses")
def get_statuses(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    statuses = get_latest_statuses()
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        statuses = {k: v for k, v in statuses.items() if k in allowed_ids}
    return {"statuses": {str(k): v for k, v in statuses.items()}}


@router.post("/scan")
async def trigger_manual_scan(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    results = await trigger_scan()
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        results = {k: v for k, v in results.items() if k in allowed_ids}
    return {"statuses": {str(k): v for k, v in results.items()}}


@router.get("/interfaces")
def get_interface_statuses(
    rack_id: int = Query(..., gt=0),
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    """
    Return live interface up/down status for every device in the rack.

    Used by the rack U-view to render port LEDs. Cached for ~25s server-side;
    offline / no-credential devices return immediately with source='unavailable'.
    """
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=404, detail="机柜不存在")

    # Filter to devices the user can access
    allowed_ids = get_user_device_ids(current_user, db)
    entries: dict[str, dict] = {}
    for d in rack.devices or []:
        if allowed_ids is not None and d.id not in allowed_ids:
            continue
        entries[str(d.id)] = interface_status.get_interface_status(d)

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat() + "Z",
        "ttl_seconds": 25,
        "devices": entries,
    }
