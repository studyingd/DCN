"""
Scripts router — batch command execution on multiple devices.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.audit_log import AuditLog
from app.models.credential import Credential
from app.models.device import Device
from app.models.user import User
from app.services.crypto import decrypt
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)
from app.services.power import _exec_ssh_command, reboot_device, shutdown_device
from app.validators import validate_script_command

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scripts"])

MAX_CONCURRENT_SSH = 10
_ssh_sem = threading.Semaphore(MAX_CONCURRENT_SSH)


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------


class ScriptExecuteRequest(BaseModel):
    device_ids: list[int]
    command: str
    timeout: int = 30
    username: str | None = None
    password: str | None = None
    credential_id: int | None = None


class DeviceResult(BaseModel):
    device_id: int
    device_name: str
    ip_address: str | None
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _resolve_creds(device: Device, body: ScriptExecuteRequest) -> tuple[str, str]:
    """Resolve SSH credentials: request-level > device-bound > defaults."""
    # Try request-level credential_id
    if body.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == body.credential_id)
                .first()
            )
            if cred:
                return (
                    cred.username or body.username or "root",
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (body.password or ""),
                )
        finally:
            db2.close()

    # Try device-bound credential
    if device.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == device.credential_id)
                .first()
            )
            if cred:
                return (
                    cred.username or body.username or "root",
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (body.password or ""),
                )
        finally:
            db2.close()

    return body.username or "root", body.password or ""


def _run_on_device(
    device: Device, command: str, timeout: int, body: ScriptExecuteRequest
) -> DeviceResult:
    """Execute a single command on one device (called inside a thread)."""
    with _ssh_sem:
        username, password = _resolve_creds(device, body)
        try:
            exit_code, out, err = _exec_ssh_command(
                device, username, password, command, timeout
            )
            return DeviceResult(
                device_id=device.id,
                device_name=device.name,
                ip_address=device.ip_address,
                success=(exit_code == 0),
                exit_code=exit_code,
                stdout=out,
                stderr=err,
            )
        except Exception as exc:
            return DeviceResult(
                device_id=device.id,
                device_name=device.name,
                ip_address=device.ip_address,
                success=False,
                error=str(exc),
            )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get("/api/scripts/devices")
def list_devices_for_scripts(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("script:manage")),
):
    """Return all devices the user can access (for device multi-select)."""
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
        }
        for d in devices
    ]


@router.post("/api/scripts/execute")
def execute_script(
    body: ScriptExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("script:manage")),
):
    """Execute a command on multiple devices concurrently."""
    if not body.device_ids:
        raise HTTPException(status_code=400, detail="请至少选择一台设备")
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="请输入要执行的命令")

    # Block obviously dangerous commands (configurable via SCRIPT_COMMAND_POLICY)
    try:
        validate_script_command(body.command)
    except ValueError as exc:
        db2 = SessionLocal()
        try:
            db2.add(
                AuditLog(
                    user_id=current_user.id,
                    username=current_user.username,
                    event_type="command_blocked",
                    command=body.command,
                    blocked=1,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db2.commit()
        finally:
            db2.close()
        raise HTTPException(status_code=400, detail=str(exc))

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

    # Execute concurrently
    results: list[DeviceResult] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SSH) as pool:
        futures = {
            pool.submit(_run_on_device, d, body.command, body.timeout, body): d.id
            for d in devices
        }
        for future in as_completed(futures):
            results.append(future.result())

    # Sort by device_id for consistent ordering
    results.sort(key=lambda r: r.device_id)

    succeeded = sum(1 for r in results if r.success)

    # Audit log — one entry per execution with per-device results as JSON
    db2 = SessionLocal()
    try:
        log = AuditLog(
            user_id=current_user.id,
            username=current_user.username,
            event_type="script_executed",
            command=body.command,
            device_name=",".join(d.name for d in devices),
            device_ip=",".join(d.ip_address or "" for d in devices),
            blocked=len(results) - succeeded,
            created_at=datetime.now(timezone.utc),
        )
        db2.add(log)
        db2.commit()
        db2.refresh(log)
        sid = f"script_{log.id}"
        log.session_id = sid
        db2.commit()
        # Per-device results detail
        detail = AuditLog(
            session_id=sid,
            user_id=current_user.id,
            username=current_user.username,
            event_type="script_result",
            command=json.dumps(
                [
                    {
                        "device_name": r.device_name,
                        "ip": r.ip_address,
                        "success": r.success,
                        "exit_code": r.exit_code,
                        "stdout": (r.stdout or "")[:2000],
                        "stderr": (r.stderr or "")[:500],
                        "error": (r.error or "")[:500],
                    }
                    for r in results
                ],
                ensure_ascii=False,
            ),
            device_name=",".join(d.name for d in devices),
            blocked=succeeded,
            created_at=datetime.now(timezone.utc),
        )
        db2.add(detail)
        db2.commit()
    finally:
        db2.close()

    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }


# ======================================================================
# Batch power control (shutdown / reboot)
#
# Why a dedicated endpoint (vs. routing through /scripts/execute):
#   - scripts.execute only speaks SSH. Many Windows hosts don't run
#     OpenSSH, but power.py already has a 3-tier fallback
#     (SSH → Win32 InitiateSystemShutdown RPC → RDP keystroke injection).
#   - Power operations need their own confirmation dialog UX and a
#     distinct audit event type so power actions don't get mixed into
#     generic script_executed metrics.
# ======================================================================

_POWER_ACTIONS = {"shutdown", "reboot"}


class PowerExecuteRequest(BaseModel):
    device_ids: list[int]
    action: str  # "shutdown" | "reboot"


class PowerDeviceResult(BaseModel):
    device_id: int
    device_name: str
    ip_address: str | None
    os_system: str | None
    success: bool
    message: str = ""


def _run_power_on_device(device: Device, action: str) -> PowerDeviceResult:
    """Run shutdown/reboot on one device in a worker thread.

    Uses device.credential_id (no per-request credential override — power
    ops should use the same trust path as the single-device power button).
    """
    # Resolve credentials from device binding
    username = "root"
    password = ""
    if device.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == device.credential_id)
                .first()
            )
            if cred:
                username = cred.username or "root"
                if cred.password_enc:
                    password = decrypt(cred.password_enc)
        finally:
            db2.close()

    try:
        if action == "shutdown":
            result = shutdown_device(device, username, password)
        else:
            result = reboot_device(device, username, password)
        # power.py returns {"success": bool, "message": str}
        return PowerDeviceResult(
            device_id=device.id,
            device_name=device.name,
            ip_address=device.ip_address,
            os_system=device.os_system,
            success=bool(result.get("success")),
            message=result.get("message", ""),
        )
    except Exception as exc:
        logger.exception("Power action %s failed on %s", action, device.ip_address)
        return PowerDeviceResult(
            device_id=device.id,
            device_name=device.name,
            ip_address=device.ip_address,
            os_system=device.os_system,
            success=False,
            message=f"执行失败: {exc}",
        )


@router.post("/api/scripts/power")
def execute_power(
    body: PowerExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("script:manage")),
):
    """Execute shutdown or reboot on multiple devices concurrently.

    For each device, the same 3-tier fallback as the single-device power
    button applies (SSH → Win32 RPC → RDP), so Windows hosts without
    OpenSSH still work.
    """
    if body.action not in _POWER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"无效操作: {body.action}")
    if not body.device_ids:
        raise HTTPException(status_code=400, detail="请至少选择一台设备")

    devices = db.query(Device).filter(Device.id.in_(body.device_ids)).all()
    device_map = {d.id: d for d in devices}
    for did in body.device_ids:
        if did not in device_map:
            raise HTTPException(status_code=404, detail=f"设备 {did} 不存在")
        if not user_can_access_device(current_user, did, db):
            raise HTTPException(
                status_code=403, detail=f"无权访问设备 {device_map[did].name}"
            )

    # Run concurrently with the same SSH semaphore used for scripts.execute
    results: list[PowerDeviceResult] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SSH) as pool:
        futures = {
            pool.submit(_run_power_on_device, d, body.action): d.id for d in devices
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r.device_id)
    succeeded = sum(1 for r in results if r.success)

    # Audit log
    db2 = SessionLocal()
    try:
        log = AuditLog(
            user_id=current_user.id,
            username=current_user.username,
            event_type=f"power_{body.action}",  # power_shutdown / power_reboot
            command=f"/api/scripts/power {body.action}",
            device_name=",".join(d.name for d in devices),
            device_ip=",".join(d.ip_address or "" for d in devices),
            blocked=len(results) - succeeded,
            created_at=datetime.now(timezone.utc),
        )
        db2.add(log)
        db2.commit()
        db2.refresh(log)
        sid = f"power_{log.id}"
        log.session_id = sid
        db2.commit()
        detail = AuditLog(
            session_id=sid,
            user_id=current_user.id,
            username=current_user.username,
            event_type="power_result",
            command=json.dumps(
                [
                    {
                        "device_name": r.device_name,
                        "ip": r.ip_address,
                        "os_system": r.os_system,
                        "success": r.success,
                        "message": (r.message or "")[:500],
                    }
                    for r in results
                ],
                ensure_ascii=False,
            ),
            device_name=",".join(d.name for d in devices),
            blocked=succeeded,
            created_at=datetime.now(timezone.utc),
        )
        db2.add(detail)
        db2.commit()
    finally:
        db2.close()

    return {
        "action": body.action,
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }
