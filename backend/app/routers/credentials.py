from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.credential import Credential
from app.models.device import Device
from app.models.user import User
from app.schemas.credential import (
    CredentialCreate,
    CredentialResponse,
    CredentialUpdate,
)
from app.services.crypto import decrypt, encrypt
from app.services.permissions import require_permission, user_can_access_device

router = APIRouter(tags=["credentials"])


def _to_response(cred: Credential, db: Session) -> CredentialResponse:
    device_count = (
        db.query(func.count(Device.id)).filter(Device.credential_id == cred.id).scalar()
        or 0
    )
    return CredentialResponse(
        id=cred.id,
        name=cred.name,
        username=cred.username,
        has_password=bool(cred.password_enc),
        has_ssh_key=bool(cred.ssh_key_enc),
        created_at=cred.created_at,
        device_count=device_count,
    )


@router.get("/api/credentials", response_model=list[CredentialResponse])
def list_credentials(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("credential:manage")),
):
    creds = db.query(Credential).order_by(Credential.id).all()
    return [_to_response(c, db) for c in creds]


@router.post(
    "/api/credentials",
    response_model=CredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_credential(
    body: CredentialCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("credential:manage")),
):
    cred = Credential(
        name=body.name,
        username=body.username,
        password_enc=encrypt(body.password) if body.password else None,
        ssh_key_enc=encrypt(body.ssh_key) if body.ssh_key else None,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return _to_response(cred, db)


# Static routes MUST come before {credential_id} routes to avoid path collision
@router.get("/api/credentials/all-devices")
def list_all_devices_for_binding(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("credential:manage")),
):
    """List all devices with their credential binding status for the binding dialog."""
    from app.services.permissions import get_user_device_ids

    allowed_ids = get_user_device_ids(current_user, db)
    query = db.query(Device).order_by(Device.id)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    devices = query.all()
    return [
        {
            "id": d.id,
            "name": d.name,
            "ip_address": d.ip_address,
            "type": d.type,
            "credential_id": d.credential_id,
            "rack_id": d.rack_id,
        }
        for d in devices
    ]


@router.get("/api/credentials/{credential_id}", response_model=CredentialResponse)
def get_credential(
    credential_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("credential:manage")),
):
    cred = db.query(Credential).filter(Credential.id == credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭据不存在")
    return _to_response(cred, db)


@router.put("/api/credentials/{credential_id}", response_model=CredentialResponse)
def update_credential(
    credential_id: int,
    body: CredentialUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("credential:manage")),
):
    cred = db.query(Credential).filter(Credential.id == credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭据不存在")

    update_data = body.model_dump(exclude_unset=True)
    if "password" in update_data:
        pw = update_data.pop("password")
        cred.password_enc = encrypt(pw) if pw else None
    if "ssh_key" in update_data:
        key = update_data.pop("ssh_key")
        cred.ssh_key_enc = encrypt(key) if key else None

    for field, value in update_data.items():
        setattr(cred, field, value)

    db.commit()
    db.refresh(cred)
    return _to_response(cred, db)


@router.delete("/api/credentials/{credential_id}")
def delete_credential(
    credential_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("credential:manage")),
):
    cred = db.query(Credential).filter(Credential.id == credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭据不存在")
    db.delete(cred)
    db.commit()
    return {"message": "凭据已删除"}


@router.get("/api/credentials/{credential_id}/decrypt")
def decrypt_credential(
    credential_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("credential:manage")),
):
    """Decrypt credential secrets. Requires admin role for production safety.
    Audit-logged on every access."""
    # Require admin role for production safety
    from app.services.permissions import is_admin_user

    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可解密凭据")

    cred = db.query(Credential).filter(Credential.id == credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭据不存在")

    # Audit-log the decryption access
    log = AuditLog(
        user_id=current_user.id,
        username=current_user.username,
        event_type="credential_decrypted",
        device_ip=f"credential:{credential_id}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    return {
        "username": cred.username,
        "password": decrypt(cred.password_enc) if cred.password_enc else "",
        "ssh_key": decrypt(cred.ssh_key_enc) if cred.ssh_key_enc else "",
    }


@router.put("/api/credentials/{credential_id}/devices")
def bind_devices(
    credential_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("credential:manage")),
):
    """Bind a credential to a set of devices. body: {device_ids: [1,2,3]}"""
    cred = db.query(Credential).filter(Credential.id == credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭据不存在")

    device_ids = body.get("device_ids", []) or []

    # Enforce device scope: a credential:manage user may only bind devices they
    # are authorized to access (prevents lateral credential abuse via IDOR).
    for did in device_ids:
        if not user_can_access_device(current_user, did, db):
            raise HTTPException(status_code=403, detail=f"无权访问设备 {did}")

    # Clear all devices currently bound to this credential
    db.query(Device).filter(Device.credential_id == credential_id).update(
        {"credential_id": None}, synchronize_session="fetch"
    )

    # Bind selected devices
    if device_ids:
        db.query(Device).filter(Device.id.in_(device_ids)).update(
            {"credential_id": credential_id}, synchronize_session="fetch"
        )

    db.commit()
    return {"message": "设备绑定已更新"}
