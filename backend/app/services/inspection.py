"""
Core inspection service — orchestrate SSH command execution, result parsing,
and database persistence for device inspections.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.credential import Credential
from app.models.device import Device
from app.models.inspection import InspectionRecord
from app.models.inspection_item import InspectionItemResult
from app.services.crypto import decrypt
from app.services.inspection_commands import get_commands, get_target_type
from app.services.inspection_parser import parse_item
from app.services.ssh import HostKeyMismatchError, connect_device, exec_ssh_command

logger = logging.getLogger(__name__)


def _resolve_creds(
    device: Device,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> tuple[str, str]:
    """Resolve SSH credentials: request-level credential → device-bound → defaults."""
    if credential_id:
        db = SessionLocal()
        try:
            cred = db.query(Credential).filter(Credential.id == credential_id).first()
            if cred:
                return (
                    cred.username or username or "root",
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (password or ""),
                )
        finally:
            db.close()

    if device.credential_id:
        db = SessionLocal()
        try:
            cred = (
                db.query(Credential)
                .filter(Credential.id == device.credential_id)
                .first()
            )
            if cred:
                return (
                    cred.username or username or "root",
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (password or ""),
                )
        finally:
            db.close()

    return username or "root", password or ""


def _exec_ssh_command(
    device: Device, username: str, password: str, command: str, timeout: int = 15
) -> tuple[int, str, str]:
    """Run a single SSH command on a device with host-key verification (TOFU)."""
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(device, username, password, db=db, timeout=timeout)
        except HostKeyMismatchError:
            return 1, "", "SSH host key mismatch — possible MITM"
        try:
            return exec_ssh_command(client, command, timeout=timeout)
        finally:
            client.close()
    finally:
        db.close()


def run_inspection(
    device: Device,
    mode: str = "standard",
    custom_items: list[str] | None = None,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
    enable_password: str | None = None,
    user_id: int | None = None,
    batch_id: str | None = None,
    timeout: int = 30,
) -> dict:
    """
    Execute a full inspection on a single device.

    Returns a dict with record_id and all item results.
    """
    target_type = get_target_type(device)
    vendor = _detect_vendor(device, target_type, username, password, credential_id)

    # Get command set
    cmds = get_commands(target_type, vendor, mode, custom_items)
    if not cmds:
        return {
            "device_id": device.id,
            "device_name": device.name,
            "ip_address": device.ip_address,
            "target_type": target_type,
            "vendor": vendor,
            "status": "failed",
            "total_items": 0,
            "normal_count": 0,
            "warning_count": 0,
            "critical_count": 0,
            "error_count": 0,
            "items": [],
            "duration_ms": 0,
            "error": "无可用巡检命令",
        }

    # Resolve credentials
    ssh_user, ssh_pass = _resolve_creds(device, credential_id, username, password)

    # Create DB record
    db = SessionLocal()
    record_id = None
    try:
        record = InspectionRecord(
            device_id=device.id,
            device_name=device.name,
            device_ip=device.ip_address,
            target_type=target_type,
            vendor=vendor,
            mode=mode,
            status="running",
            total_items=len(cmds),
            started_at=datetime.now(timezone.utc),
            triggered_by=user_id,
            batch_id=batch_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id
    finally:
        db.close()

    # Execute commands and parse results
    item_results = []
    start_time = time.time()

    for cmd_info in cmds:
        item_start = time.time()
        item_type = cmd_info["item_type"]
        command = cmd_info["command"]
        cmd_timeout = min(cmd_info["timeout"], timeout)

        try:
            exit_code, stdout, stderr = _exec_ssh_command(
                device,
                ssh_user,
                ssh_pass,
                command,
                cmd_timeout,
            )
            raw_output = stdout or stderr
            parsed = parse_item(item_type, raw_output, target_type)
            parsed["success"] = parsed.get("success", True) and exit_code == 0
            if not parsed["success"] and stderr:
                parsed["error_message"] = stderr[:500]
        except Exception as exc:
            logger.warning(
                "Inspection command failed: %s on %s: %s", item_type, device.name, exc
            )
            parsed = {
                "success": False,
                "value": None,
                "unit": None,
                "status": "error",
                "details": None,
                "error_message": str(exc)[:500],
            }
            raw_output = ""

        item_duration = int((time.time() - item_start) * 1000)
        parsed["item_type"] = item_type
        parsed["command_used"] = command
        parsed["raw_output"] = (raw_output or "")[:5000]
        parsed["duration_ms"] = item_duration
        item_results.append(parsed)

    total_duration = int((time.time() - start_time) * 1000)

    # Aggregate counts
    counts = {"normal": 0, "warning": 0, "critical": 0, "error": 0}
    for r in item_results:
        status = r.get("status", "error")
        counts[status] = counts.get(status, 0) + 1

    # Determine overall status
    if counts["error"] == len(item_results):
        overall_status = "failed"
    elif counts["error"] + counts["critical"] > len(item_results) / 2:
        overall_status = "failed"
    elif counts["error"] > 0 or counts["critical"] > 0:
        overall_status = "partial"
    else:
        overall_status = "completed"

    # Update DB record and persist items
    db = SessionLocal()
    try:
        record = (
            db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
        )
        if record:
            record.status = overall_status
            record.normal_count = counts.get("normal", 0)
            record.warning_count = counts.get("warning", 0)
            record.critical_count = counts.get("critical", 0)
            record.error_count = counts.get("error", 0)
            record.finished_at = datetime.now(timezone.utc)
            record.duration_ms = total_duration

            # Persist item results
            for r in item_results:
                item = InspectionItemResult(
                    record_id=record_id,
                    item_type=r["item_type"],
                    success=r.get("success", False),
                    value=r.get("value"),
                    unit=r.get("unit"),
                    status=r.get("status", "error"),
                    details=r.get("details"),
                    raw_output=r.get("raw_output"),
                    error_message=r.get("error_message"),
                    command_used=r.get("command_used"),
                    executed_at=datetime.now(timezone.utc),
                    duration_ms=r.get("duration_ms"),
                )
                db.add(item)

            db.commit()
    finally:
        db.close()

    return {
        "device_id": device.id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "target_type": target_type,
        "vendor": vendor,
        "record_id": record_id,
        "status": overall_status,
        "total_items": len(item_results),
        "normal_count": counts.get("normal", 0),
        "warning_count": counts.get("warning", 0),
        "critical_count": counts.get("critical", 0),
        "error_count": counts.get("error", 0),
        "duration_ms": total_duration,
        "items": item_results,
    }


def run_batch_inspection(
    device_ids: list[int],
    mode: str = "standard",
    custom_items: list[str] | None = None,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
    enable_password: str | None = None,
    user_id: int | None = None,
    timeout: int = 30,
) -> list[dict]:
    """
    Sequential batch inspection. Each device is inspected one at a time.
    All records share a batch_id for grouping.
    """
    batch_id = str(uuid.uuid4())
    results = []

    db = SessionLocal()
    try:
        devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
        device_map = {d.id: d for d in devices}
    finally:
        db.close()

    for did in device_ids:
        device = device_map.get(did)
        if not device:
            results.append(
                {
                    "device_id": did,
                    "device_name": f"unknown-{did}",
                    "ip_address": None,
                    "target_type": "unknown",
                    "vendor": None,
                    "status": "failed",
                    "total_items": 0,
                    "normal_count": 0,
                    "warning_count": 0,
                    "critical_count": 0,
                    "error_count": 0,
                    "duration_ms": 0,
                    "items": [],
                    "error": "设备不存在",
                }
            )
            continue

        if not device.ip_address:
            results.append(
                {
                    "device_id": did,
                    "device_name": device.name,
                    "ip_address": None,
                    "target_type": get_target_type(device),
                    "vendor": None,
                    "status": "failed",
                    "total_items": 0,
                    "normal_count": 0,
                    "warning_count": 0,
                    "critical_count": 0,
                    "error_count": 0,
                    "duration_ms": 0,
                    "items": [],
                    "error": "设备未配置IP地址",
                }
            )
            continue

        try:
            result = run_inspection(
                device=device,
                mode=mode,
                custom_items=custom_items,
                credential_id=credential_id,
                username=username,
                password=password,
                enable_password=enable_password,
                user_id=user_id,
                batch_id=batch_id,
                timeout=timeout,
            )
            results.append(result)
        except Exception as exc:
            logger.exception("Batch inspection failed for device %s", device.name)
            results.append(
                {
                    "device_id": did,
                    "device_name": device.name,
                    "ip_address": device.ip_address,
                    "target_type": get_target_type(device),
                    "vendor": None,
                    "status": "failed",
                    "total_items": 0,
                    "normal_count": 0,
                    "warning_count": 0,
                    "critical_count": 0,
                    "error_count": 0,
                    "duration_ms": 0,
                    "items": [],
                    "error": str(exc)[:200],
                }
            )

    return results


def _detect_vendor(
    device: Device,
    target_type: str,
    username: str | None,
    password: str | None,
    credential_id: int | None,
) -> str | None:
    """
    For network devices, try to detect vendor from prompt or version output.
    For servers, return None.
    """
    if target_type != "network":
        return None

    # Try to detect vendor from device.type naming conventions
    # If device.os_system contains vendor info, use it
    os_lower = (device.os_system or "").lower()
    if "huawei" in os_lower or "vrp" in os_lower:
        return "huawei"
    if "cisco" in os_lower or "ios" in os_lower:
        return "cisco"
    if "h3c" in os_lower or "comware" in os_lower:
        return "h3c"
    if "ruijie" in os_lower or "rgos" in os_lower:
        return "ruijie"
    if "zte" in os_lower:
        return "zte"

    # Default to huawei for network devices
    return "huawei"
