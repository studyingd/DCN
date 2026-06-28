"""Automatic cleanup of expired audit logs and session recordings."""

import logging
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.session_recording import SessionRecording
from app.services.settings import get_setting

logger = logging.getLogger(__name__)


def purge_expired_data():
    """Delete audit logs and recordings older than their retention days."""
    db = SessionLocal()
    try:
        # --- Purge audit logs ---
        audit_days = int(get_setting(db, "audit_retention_days") or "90")
        if audit_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=audit_days)
            count = (
                db.query(AuditLog)
                .filter(AuditLog.created_at < cutoff)
                .delete(synchronize_session="fetch")
            )
            if count:
                db.commit()
                logger.info(
                    "Purged %d audit logs older than %d days", count, audit_days
                )

        # --- Purge session recordings ---
        rec_days = int(get_setting(db, "recording_retention_days") or "90")
        if rec_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=rec_days)
            # Only purge finished recordings (ended_at set) to avoid racing an
            # active session whose recorder has not finalized yet.
            rows = (
                db.query(SessionRecording)
                .filter(
                    SessionRecording.started_at < cutoff,
                    SessionRecording.ended_at != None,  # noqa: E711
                )
                .all()
            )
            # Delete the MinIO objects too, so they do not become orphans.
            from app.services.storage import delete_recording

            for rec in rows:
                if rec.session_id:
                    try:
                        delete_recording(rec.session_id)
                    except Exception as e:
                        logger.warning(
                            "Failed to delete recording object %s: %s",
                            rec.session_id,
                            e,
                        )
            count = (
                db.query(SessionRecording)
                .filter(SessionRecording.id.in_([r.id for r in rows]))
                .delete(synchronize_session="fetch")
            ) if rows else 0
            if count:
                db.commit()
                logger.info("Purged %d recordings older than %d days", count, rec_days)
    except Exception as e:
        logger.error("Retention purge failed: %s", e)
        raise
    finally:
        db.close()
