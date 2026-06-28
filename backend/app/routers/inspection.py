"""
Inspection router — device inspection execution, history, and reports.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.device import Device
from app.models.inspection import InspectionRecord
from app.models.user import User
from app.schemas.inspection import (
    InspectionItemInfo,
    InspectionItemsCatalogResponse,
    InspectionRecordDetail,
    InspectionRecordListResponse,
    InspectionRecordResponse,
    InspectionReportResponse,
    InspectionRunRequest,
)
from app.services.inspection import run_batch_inspection
from app.services.inspection_commands import get_item_catalog, get_target_type
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inspection"])


@router.get("/api/inspection/devices")
def list_devices_for_inspection(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Return all devices the user can access, with target_type for inspection."""
    query = db.query(Device).order_by(Device.name)
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    devices = query.all()
    return [
        {
            "id": d.id,
            "name": d.name,
            "ip_address": d.ip_address,
            "type": d.type,
            "status": d.status,
            "os_system": d.os_system,
            "credential_id": d.credential_id,
            "target_type": get_target_type(d),
        }
        for d in devices
    ]


@router.get("/api/inspection/items/{target_type}")
def get_items_catalog(
    target_type: str,
    _current_user: User = Depends(require_permission("inspection:manage")),
):
    """Return available inspection items for the given target type."""
    if target_type not in ("network", "linux", "windows"):
        raise HTTPException(
            status_code=400, detail="target_type 必须是 network/linux/windows"
        )
    items = get_item_catalog(target_type)
    return InspectionItemsCatalogResponse(
        target_type=target_type,
        items=[InspectionItemInfo(**i) for i in items],
    )


@router.post("/api/inspection/run")
def run_inspection(
    body: InspectionRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Execute inspection on one or more devices."""
    # Load devices and verify access
    devices = db.query(Device).filter(Device.id.in_(body.device_ids)).all()
    device_map = {d.id: d for d in devices}

    for did in body.device_ids:
        if did not in device_map:
            raise HTTPException(status_code=404, detail=f"设备 {did} 不存在")
        if not user_can_access_device(current_user, did, db):
            raise HTTPException(
                status_code=403, detail=f"无权访问设备 {device_map[did].name}"
            )

    # Execute (sequential batch)
    results = run_batch_inspection(
        device_ids=body.device_ids,
        mode=body.mode,
        custom_items=body.items,
        credential_id=body.credential_id,
        username=body.username,
        password=body.password,
        enable_password=body.enable_password,
        user_id=current_user.id,
        timeout=body.timeout,
    )

    succeeded = sum(1 for r in results if r["status"] != "failed")
    batch_id = results[0].get("batch_id") if results else None

    # Audit log
    audit = AuditLog(
        user_id=current_user.id,
        username=current_user.username,
        event_type="inspection_run",
        command=f"mode={body.mode}, devices={len(body.device_ids)}",
        device_name=",".join(d.name for d in devices),
        created_at=datetime.now(timezone.utc),
    )
    db.add(audit)
    db.commit()

    return {
        "batch_id": batch_id,
        "results": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }


@router.get("/api/inspection/records")
def list_records(
    device_id: int | None = Query(None),
    target_type: str | None = Query(None),
    status: str | None = Query(None),
    batch_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Paginated inspection history."""
    query = db.query(InspectionRecord).order_by(InspectionRecord.started_at.desc())

    # Apply device access filter
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(InspectionRecord.device_id.in_(allowed_ids))

    if device_id:
        query = query.filter(InspectionRecord.device_id == device_id)
    if target_type:
        query = query.filter(InspectionRecord.target_type == target_type)
    if status:
        query = query.filter(InspectionRecord.status == status)
    if batch_id:
        query = query.filter(InspectionRecord.batch_id == batch_id)

    total = query.count()
    records = query.offset((page - 1) * page_size).limit(page_size).all()

    return InspectionRecordListResponse(
        items=[InspectionRecordResponse.model_validate(r) for r in records],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/api/inspection/records/{record_id}")
def get_record_detail(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Get full detail of a single inspection, including all item results."""
    record = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="巡检记录不存在")

    # Check device access
    if not user_can_access_device(current_user, record.device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该巡检记录")

    return InspectionRecordDetail.model_validate(record)


@router.delete("/api/inspection/records/{record_id}")
def delete_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Delete a single inspection record (cascades to items)."""
    record = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="巡检记录不存在")

    # Verify device-level access control
    from app.services.permissions import user_can_access_device

    if not user_can_access_device(current_user, record.device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")

    db.delete(record)
    db.commit()
    return {"message": "巡检记录已删除"}


@router.get("/api/inspection/report")
def get_report(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    target_type: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("inspection:manage")),
):
    """Aggregate inspection statistics for the report tab."""
    query = db.query(InspectionRecord)

    # Apply device access filter
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(InspectionRecord.device_id.in_(allowed_ids))

    if target_type:
        query = query.filter(InspectionRecord.target_type == target_type)

    if start_date:
        try:
            sd = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            query = query.filter(InspectionRecord.started_at >= sd)
        except ValueError:
            pass
    if end_date:
        try:
            ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            query = query.filter(InspectionRecord.started_at <= ed)
        except ValueError:
            pass

    records = query.all()
    total = len(records)

    if total == 0:
        return InspectionReportResponse()

    total_items = sum(r.total_items for r in records)
    normal = sum(r.normal_count for r in records)
    warning = sum(r.warning_count for r in records)
    critical = sum(r.critical_count for r in records)
    error = sum(r.error_count for r in records)

    def _pct(val):
        return round(val / total_items * 100, 1) if total_items > 0 else 0

    # By target type
    by_type: dict[str, dict] = {}
    type_records: dict[str, list] = {}
    for r in records:
        t = r.target_type
        type_records.setdefault(t, []).append(r)

    for t, recs in type_records.items():
        t_items = sum(r.total_items for r in recs)
        t_warnings = sum(r.warning_count for r in recs)
        t_critical = sum(r.critical_count for r in recs)
        by_type[t] = {
            "count": len(recs),
            "warning_rate": round(t_warnings / t_items * 100, 1) if t_items > 0 else 0,
            "critical_rate": round(t_critical / t_items * 100, 1) if t_items > 0 else 0,
        }

    # Top warnings (most recent records with warnings/critical)
    top_warnings = []
    warning_records = sorted(
        [r for r in records if r.warning_count + r.critical_count > 0],
        key=lambda r: r.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:10]
    for r in warning_records:
        top_warnings.append(
            {
                "device_name": r.device_name,
                "target_type": r.target_type,
                "warning_count": r.warning_count,
                "critical_count": r.critical_count,
                "timestamp": r.started_at.isoformat() + "Z" if r.started_at else None,
            }
        )

    # Trend by day (last 30 days)
    trend: dict[str, dict] = {}
    for r in records:
        if r.started_at:
            day = r.started_at.strftime("%Y-%m-%d")
            if day not in trend:
                trend[day] = {"normal": 0, "warning": 0, "critical": 0, "error": 0}
            trend[day]["normal"] += r.normal_count
            trend[day]["warning"] += r.warning_count
            trend[day]["critical"] += r.critical_count
            trend[day]["error"] += r.error_count

    trend_list = [{"date": day, **counts} for day, counts in sorted(trend.items())]

    return InspectionReportResponse(
        total_inspections=total,
        total_items_checked=total_items,
        overall_health={
            "normal_pct": _pct(normal),
            "warning_pct": _pct(warning),
            "critical_pct": _pct(critical),
            "error_pct": _pct(error),
        },
        by_target_type=by_type,
        top_warnings=top_warnings,
        trend=trend_list,
    )
