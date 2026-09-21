"""
Scripts router — batch command execution on multiple devices.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.rate_limiter import limiter
from app.models.device import Device
from app.models.user import User
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
    user_can_use_credential,
)
from app.services.script_exec import (
    MAX_CONCURRENT_SSH,
    DeviceResult,
    PowerDeviceResult,
    run_power_on_device,
    run_script_on_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scripts"])


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------


class ScriptExecuteRequest(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=500)
    command: str = Field(min_length=1, max_length=32768)
    timeout: int = Field(default=30, ge=1, le=300)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    ssh_key: str | None = Field(default=None, max_length=16384)
    credential_id: int | None = None

    @field_validator("device_ids")
    @classmethod
    def _unique_devices(cls, value):
        return list(dict.fromkeys(value))


# 执行内核(DeviceResult/PowerDeviceResult/run_script_on_device/
# run_power_on_device/SSH 并发信号量)已下沉到 services/script_exec.py:
# automation 与 scheduler 作为 service 层不应反向 import router 的私有函数。


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get("/api/scripts/devices")
def list_devices_for_scripts(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
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
            "credential_id": None,
        }
        for d in devices
    ]


@router.post("/api/scripts/execute")
# 一次请求会向多台设备并发下发命令，成本随目标数放大，单独收紧。
@limiter.limit("30/minute")
def execute_script(
    body: ScriptExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """Execute a command on multiple devices concurrently."""
    # device_ids 为空由 ScriptExecuteRequest 的 Field(min_length=1) 拦成 422，
    # 这里只需校验能通过 min_length 但内容为空白的命令。
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="请输入要执行的命令")

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
        if not user_can_use_credential(
            current_user, device_map[did], body.credential_id, db
        ):
            raise HTTPException(
                status_code=403, detail=f"凭据未绑定到设备 {device_map[did].name}"
            )

    # Execute concurrently
    results: list[DeviceResult] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SSH) as pool:
        futures = {
            pool.submit(
                run_script_on_device,
                d,
                body.command,
                body.timeout,
                username=body.username,
                password=body.password,
                ssh_key=body.ssh_key,
            ): d.id
            for d in devices
        }
        for future in as_completed(futures):
            results.append(future.result())

    # Sort by device_id for consistent ordering
    results.sort(key=lambda r: r.device_id)

    succeeded = sum(1 for r in results if r.success)

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
#   - 电源操作走 power.py 的通道约定(Windows→WinRM,Linux→SSH),
#     且发出指令后会探测端口确认设备真实离线;
#   - Power operations need their own confirmation dialog UX.
# ======================================================================

_POWER_ACTIONS = {"shutdown", "reboot"}


class PowerExecuteRequest(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=500)
    action: str = Field(min_length=1, max_length=16)  # "shutdown" | "reboot"
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    ssh_key: str | None = Field(default=None, max_length=16384)

    @field_validator("device_ids")
    @classmethod
    def _unique_power_devices(cls, value):
        return list(dict.fromkeys(value))


@router.post("/api/scripts/power")
# 关机/重启不可逆，限额比命令执行更严，降低令牌被盗后的破坏面。
@limiter.limit("10/minute")
def execute_power(
    body: PowerExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """Execute shutdown or reboot on multiple devices concurrently.

    每台设备按 power.py 的通道约定执行(Windows→WinRM,Linux→SSH),
    指令发出后探测端口确认真实离线。
    """
    if body.action not in _POWER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"无效操作: {body.action}")
    # device_ids 为空由 PowerExecuteRequest 的 Field(min_length=1) 拦成 422。

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
            pool.submit(
                run_power_on_device,
                d,
                body.action,
                username=body.username,
                password=body.password,
                ssh_key=body.ssh_key,
            ): d.id
            for d in devices
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r.device_id)
    succeeded = sum(1 for r in results if r.success)

    return {
        "action": body.action,
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }
