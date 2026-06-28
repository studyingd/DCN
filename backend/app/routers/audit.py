import json
from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.session_recording import SessionRecording
from app.models.user import User
from app.services.auth import get_current_user
from app.services.permissions import require_permission, user_has_permission
from app.services.settings import get_all_settings, set_setting
from app.services.storage import download_recording
from app.utils import utc_isoformat

router = APIRouter(tags=["audit"])


def _parse_date_range(start_date: str | None, end_date: str | None):
    """Validate and parse date strings into datetime objects.

    Returns (start_dt, end_dt) where start_dt is the beginning of start_date
    and end_dt is the end of end_date (23:59:59.999999).
    Raises HTTPException(400) if either date string is invalid.
    """
    start_dt = None
    end_dt = None
    if start_date:
        try:
            d = datetime.fromisoformat(start_date)
            start_dt = datetime.combine(d.date(), time.min, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail=f"无效的开始日期格式: {start_date}"
            )
    if end_date:
        try:
            d = datetime.fromisoformat(end_date)
            end_dt = datetime.combine(d.date(), time.max, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail=f"无效的结束日期格式: {end_date}"
            )
    return start_dt, end_dt


# ---- Settings ----


class SettingUpdate(BaseModel):
    settings: dict[str, str]


_ALLOWED_SETTINGS = {
    "audit_enabled",
    "recording_enabled",
    "command_interception_enabled",
    "audit_retention_days",
    "recording_retention_days",
    "blocked_commands",
}


@router.get("/api/settings")
def list_settings(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    return get_all_settings(db)


@router.put("/api/settings")
def update_settings(
    body: SettingUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    for key, value in body.settings.items():
        if key not in _ALLOWED_SETTINGS:
            raise HTTPException(status_code=400, detail=f"不允许的设置项: {key}")
        set_setting(db, key, value)
    return get_all_settings(db)


# ---- Audit Users (for filter dropdowns) ----


@router.get("/api/audit-users")
def list_audit_users(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    """Return distinct usernames from audit_logs for filter dropdowns."""
    rows = db.query(AuditLog.username).distinct().order_by(AuditLog.username).all()
    return [r[0] for r in rows if r[0]]


# ---- Audit Sessions (grouped by session_id) ----


@router.get("/api/audit-sessions")
def list_audit_sessions(
    username: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List sessions grouped by session_id. Non-admin users only see their own."""
    if not user_has_permission(current_user, "audit:manage", db):
        username = current_user.username
        limit = min(limit, 10)

    start_dt, end_dt = _parse_date_range(start_date, end_date)

    starts = db.query(AuditLog).filter(AuditLog.event_type == "session_start")
    if username:
        starts = starts.filter(AuditLog.username == username)
    if start_dt:
        starts = starts.filter(AuditLog.created_at >= start_dt)
    if end_dt:
        starts = starts.filter(AuditLog.created_at <= end_dt)
    starts = starts.order_by(desc(AuditLog.id)).limit(limit).all()

    if not starts:
        return []

    session_ids = [s.session_id for s in starts if s.session_id]

    # Batch query for end times
    end_logs = (
        db.query(AuditLog.session_id, AuditLog.created_at)
        .filter(
            AuditLog.session_id.in_(session_ids), AuditLog.event_type == "session_end"
        )
        .all()
    )
    end_map = {r[0]: r[1] for r in end_logs}

    # Batch query for command counts
    cmd_counts = (
        db.query(AuditLog.session_id, func.count(AuditLog.id))
        .filter(
            AuditLog.session_id.in_(session_ids),
            AuditLog.event_type == "command_executed",
        )
        .group_by(AuditLog.session_id)
        .all()
    )
    cmd_map = dict(cmd_counts)

    # Batch query for recording durations (more accurate than ended_at - started_at)
    rec_durations = (
        db.query(SessionRecording.session_id, SessionRecording.duration_seconds)
        .filter(SessionRecording.session_id.in_(session_ids))
        .all()
    )
    duration_map = {r[0]: r[1] for r in rec_durations if r[1] is not None}

    blocked_counts = (
        db.query(AuditLog.session_id, func.count(AuditLog.id))
        .filter(
            AuditLog.session_id.in_(session_ids),
            AuditLog.event_type == "command_blocked",
        )
        .group_by(AuditLog.session_id)
        .all()
    )
    blocked_map = dict(blocked_counts)

    result = []
    for s in starts:
        sid = s.session_id
        ended_at = end_map.get(sid)
        cmd_count = cmd_map.get(sid, 0)
        blocked_count = blocked_map.get(sid, 0)

        duration = duration_map.get(sid)
        if duration is None and s.created_at and ended_at:
            duration = int((ended_at - s.created_at).total_seconds())

        started = s.created_at
        date_part = started.strftime("%Y%m%d") if started else "unknown"
        time_part = started.strftime("%H%M%S") if started else "unknown"
        display_name = f"{s.username or 'unknown'}-{s.device_name or 'unknown'}-{date_part}-{time_part}"

        result.append(
            {
                "session_id": sid,
                "username": s.username,
                "device_name": s.device_name,
                "device_ip": s.device_ip,
                "started_at": utc_isoformat(s.created_at),
                "ended_at": utc_isoformat(ended_at),
                "duration_seconds": duration,
                "command_count": cmd_count,
                "blocked_count": blocked_count,
                "display_name": display_name,
            }
        )

    return result


@router.get("/api/audit-sessions/{session_id}/commands")
def list_session_commands(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    """List all commands (executed + blocked) for a given session."""
    logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.session_id == session_id,
            AuditLog.event_type.in_(["command_executed", "command_blocked"]),
        )
        .order_by(AuditLog.id.asc())
        .all()
    )
    return [
        {
            "id": l.id,
            "event_type": l.event_type,
            "command": l.command,
            "blocked": bool(l.blocked),
            "created_at": utc_isoformat(l.created_at),
        }
        for l in logs
    ]


# ---- Audit Logs (flat, original) ----


@router.get("/api/audit-logs")
def list_audit_logs(
    device_id: int | None = None,
    event_type: str | None = None,
    username: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    q = db.query(AuditLog)
    if device_id:
        q = q.filter(AuditLog.device_id == device_id)
    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    if username:
        q = q.filter(AuditLog.username == username)
    logs = q.order_by(desc(AuditLog.id)).limit(limit).all()
    return [
        {
            "id": l.id,
            "session_id": l.session_id,
            "device_id": l.device_id,
            "device_name": l.device_name,
            "device_ip": l.device_ip,
            "username": l.username,
            "event_type": l.event_type,
            "command": l.command,
            "blocked": bool(l.blocked),
            "created_at": utc_isoformat(l.created_at),
        }
        for l in logs
    ]


# ---- Session Recordings ----


@router.get("/api/recordings")
def list_recordings(
    username: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    q = db.query(SessionRecording)
    if username:
        q = q.filter(SessionRecording.username == username)
    start_dt, end_dt = _parse_date_range(start_date, end_date)
    if start_dt:
        q = q.filter(SessionRecording.started_at >= start_dt)
    if end_dt:
        q = q.filter(SessionRecording.started_at <= end_dt)
    recs = q.order_by(desc(SessionRecording.id)).limit(50).all()
    user_ids = {r.user_id for r in recs if r.user_id}
    user_map: dict[int, str] = {}
    if user_ids:
        from app.models.user import User as UserModel

        rows = (
            db.query(UserModel.id, UserModel.username)
            .filter(UserModel.id.in_(user_ids))
            .all()
        )
        user_map = {row.id: row.username for row in rows}
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "device_name": r.device_name,
            "device_ip": r.device_ip,
            "username": r.username,
            "operator": user_map.get(r.user_id, "") if r.user_id else "",
            "conn_type": r.conn_type,
            "file_size": r.file_size,
            "started_at": utc_isoformat(r.started_at),
            "ended_at": utc_isoformat(r.ended_at),
            "duration_seconds": r.duration_seconds,
        }
        for r in recs
    ]


@router.get("/api/recordings/{recording_id}/download")
def get_recording_data(
    recording_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("audit:manage")),
):
    rec = db.query(SessionRecording).filter(SessionRecording.id == recording_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="录像不存在")

    if rec.conn_type == "rdp":
        # RDP recordings are streamed via WebSocket — return metadata only
        return {"data": "", "conn_type": "rdp", "session_id": rec.session_id}

    data = download_recording(rec.session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="录像文件不存在")
    return {"data": data.decode("utf-8")}


# ---- Login History ----


@router.get("/api/login-history")
def list_login_history(
    username: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List system login events. Non-admin users only see their own."""
    if not user_has_permission(current_user, "audit:manage", db):
        username = current_user.username
        limit = min(limit, 10)

    start_dt, end_dt = _parse_date_range(start_date, end_date)
    q = db.query(AuditLog).filter(AuditLog.event_type == "system_login")
    if username:
        q = q.filter(AuditLog.username == username)
    if start_dt:
        q = q.filter(AuditLog.created_at >= start_dt)
    if end_dt:
        q = q.filter(AuditLog.created_at <= end_dt)
    logs = q.order_by(desc(AuditLog.id)).limit(limit).all()
    return [
        {
            "id": l.id,
            "username": l.username,
            "ip": l.device_ip,
            "created_at": utc_isoformat(l.created_at),
        }
        for l in logs
    ]


# ---- Script Records ----


@router.get("/api/script-records")
def list_script_records(
    username: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List script execution records. Non-admin users only see their own."""
    if not user_has_permission(current_user, "audit:manage", db):
        username = current_user.username

    start_dt, end_dt = _parse_date_range(start_date, end_date)
    q = db.query(AuditLog).filter(
        AuditLog.event_type.in_(["script_executed", "power_shutdown", "power_reboot"])
    )
    if username:
        q = q.filter(AuditLog.username == username)
    if start_dt:
        q = q.filter(AuditLog.created_at >= start_dt)
    if end_dt:
        q = q.filter(AuditLog.created_at <= end_dt)
    logs = q.order_by(desc(AuditLog.id)).limit(limit).all()

    if not logs:
        return []

    sids = [l.session_id for l in logs if l.session_id]

    # Batch load per-device results
    detail_logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.session_id.in_(sids),
            AuditLog.event_type.in_(["script_result", "power_result"]),
        )
        .all()
    )
    detail_map = {d.session_id: d for d in detail_logs}

    result = []
    for l in logs:
        detail = detail_map.get(l.session_id)
        device_results = []
        if detail and detail.command:
            try:
                device_results = json.loads(detail.command)
            except (json.JSONDecodeError, TypeError):
                pass

        device_names = l.device_name.split(",") if l.device_name else []
        succeeded = detail.blocked if detail else 0
        failed_count = l.blocked if l.blocked else 0

        result.append(
            {
                "id": l.id,
                "username": l.username,
                "command": l.command,
                "devices": device_names,
                "total": len(device_names),
                "succeeded": succeeded,
                "failed": failed_count,
                "created_at": utc_isoformat(l.created_at),
                "device_results": device_results,
            }
        )

    return result
