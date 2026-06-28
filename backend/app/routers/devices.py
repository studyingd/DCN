from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.credential import Credential
from app.models.device import Device
from app.models.rack import Rack
from app.models.user import User
from app.schemas.device import (
    DeviceCreate,
    DeviceResponse,
    DeviceUpdate,
    OSDetectRequest,
    OSDetectResponse,
)
from app.services.crypto import decrypt
from app.services.os_detect import detect_os
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)
from app.services.power import reboot_device, shutdown_device
from app.utils import apply_update

router = APIRouter(tags=["devices"])


@router.get("/api/racks/{rack_id}/devices", response_model=list[DeviceResponse])
def list_devices(
    rack_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    query = db.query(Device).filter(Device.rack_id == rack_id)
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    devices = query.order_by(Device.id).all()
    return [DeviceResponse.model_validate(d) for d in devices]


@router.post(
    "/api/racks/{rack_id}/devices",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_device(
    rack_id: int,
    body: DeviceCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    device = Device(
        rack_id=rack_id,
        name=body.name,
        type=body.type,
        ip_address=body.ip_address,
        purpose=body.purpose,
        os_system=body.os_system,
        position_u=body.position_u,
        size_u=body.size_u,
        ssh_port=body.ssh_port,
        rdp_port=body.rdp_port,
        web_url=body.web_url,
        owner=body.owner,
        credential_id=body.credential_id,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return DeviceResponse.model_validate(device)


@router.get("/api/devices/{device_id}", response_model=DeviceResponse)
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    return DeviceResponse.model_validate(device)


@router.put("/api/devices/{device_id}", response_model=DeviceResponse)
def update_device(
    device_id: int,
    body: DeviceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    update_data = body.model_dump(exclude_unset=True)
    apply_update(
        device,
        update_data,
        [
            "name",
            "type",
            "ip_address",
            "purpose",
            "os_system",
            "position_u",
            "size_u",
            "ssh_port",
            "rdp_port",
            "web_url",
            "mac_address",
            "owner",
            "credential_id",
        ],
    )
    db.commit()
    db.refresh(device)
    return DeviceResponse.model_validate(device)


@router.delete("/api/devices/{device_id}")
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    db.delete(device)
    db.commit()
    return {"message": "设备已删除"}


# ------------------------------------------------------------------
# OS Detection
# ------------------------------------------------------------------


@router.post("/api/devices/detect-os", response_model=OSDetectResponse)
async def detect_device_os(
    body: OSDetectRequest,
    current_user: User = Depends(require_permission("device:manage")),
):
    """Detect the operating system of a device by its IP address.

    Probes SSH (22), RDP (3389), and SMB (445) ports.
    Parses the SSH banner to identify OS type and version.
    If a credential_id is provided and SSH is open,
    logs in and reads /etc/os-release for exact Linux distro name.
    """
    username = None
    password = None
    if body.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == body.credential_id)
                .first()
            )
            if cred:
                username = cred.username
                password = decrypt(cred.password_enc) if cred.password_enc else ""
        finally:
            db2.close()

    result = await detect_os(
        body.ip_address,
        username=username,
        password=password,
    )
    return OSDetectResponse(**result.to_dict())


# ------------------------------------------------------------------
# Power control
# ------------------------------------------------------------------

_POWER_ACTIONS = {"shutdown", "reboot"}


class PowerRequest(BaseModel):
    action: str  # shutdown / reboot
    username: str | None = None
    password: str | None = None
    credential_id: int | None = None


def _resolve_creds_for_power(device: Device, body: PowerRequest) -> tuple[str, str]:
    """Resolve SSH credentials from request body or device's bound credential."""
    if body.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == body.credential_id)
                .first()
            )
            if cred:
                u = cred.username or body.username or "root"
                p = (
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (body.password or "")
                )
                return u, p
        finally:
            db2.close()

    if device.credential_id:
        db2 = SessionLocal()
        try:
            cred = (
                db2.query(Credential)
                .filter(Credential.id == device.credential_id)
                .first()
            )
            if cred:
                u = cred.username or body.username or "root"
                p = (
                    decrypt(cred.password_enc)
                    if cred.password_enc
                    else (body.password or "")
                )
                return u, p
        finally:
            db2.close()

    return body.username or "root", body.password or ""


@router.post("/api/devices/{device_id}/power")
def power_control(
    device_id: int,
    body: PowerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    if body.action not in _POWER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {body.action}")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")

    username, password = _resolve_creds_for_power(device, body)

    if body.action == "shutdown":
        return shutdown_device(device, username, password)
    if body.action == "reboot":
        return reboot_device(device, username, password)
