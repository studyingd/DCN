from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.monitor import get_latest_statuses, trigger_scan
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


@router.get("/statuses")
def get_statuses(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    statuses = get_latest_statuses()
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        statuses = {k: v for k, v in statuses.items() if k in allowed_ids}
    return {
        "statuses": {
            str(k): "online" if v == "online" else "offline"
            for k, v in statuses.items()
        }
    }


@router.post("/scan")
async def trigger_manual_scan(
    current_user: User = Depends(require_permission("device:view")),
    db: Session = Depends(get_db),
):
    results = await trigger_scan()
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        results = {k: v for k, v in results.items() if k in allowed_ids}
    return {
        "statuses": {
            str(k): "online" if v == "online" else "offline" for k, v in results.items()
        }
    }
