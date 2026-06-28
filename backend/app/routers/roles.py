import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.role import Role
from app.models.role_device_access import RoleDeviceAccess
from app.models.user import User
from app.schemas.role import RoleCreate, RoleResponse, RoleUpdate
from app.services.permissions import PERMISSION_GROUPS, PERMISSIONS, require_permission
from app.utils import apply_update

router = APIRouter(tags=["roles"])


@router.get("/api/permissions")
def list_permissions(
    _admin: User = Depends(require_permission("user:manage")),
):
    return {
        "permissions": [{"key": k, "label": v} for k, v in PERMISSIONS.items()],
        "groups": PERMISSION_GROUPS,
    }


@router.post("/api/roles", response_model=RoleResponse)
def create_role(
    body: RoleCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    if db.query(Role).filter(Role.name == body.name).first():
        raise HTTPException(400, detail="角色名称已存在")
    role = Role(
        name=body.name,
        description=body.description,
        permissions=json.dumps(body.permissions),
        device_scope=body.device_scope,
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    _sync_device_access(role, body.device_ids, db)
    return _role_response(role, db)


@router.get("/api/roles", response_model=list[RoleResponse])
def list_roles(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    roles = db.query(Role).order_by(Role.id).all()
    return [_role_response(r, db) for r in roles]


@router.get("/api/roles/{role_id}", response_model=RoleResponse)
def get_role(
    role_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(404, detail="角色不存在")
    return _role_response(role, db)


@router.put("/api/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: int,
    body: RoleUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(404, detail="角色不存在")
    data = body.model_dump(exclude_unset=True)
    if "permissions" in data:
        data["permissions"] = json.dumps(data["permissions"])
    device_ids = data.pop("device_ids", None)
    # is_admin is intentionally NOT settable here — it is a system flag managed
    # only via DB/migration, never via the role update API.
    apply_update(role, data, ["name", "description", "permissions", "device_scope"])
    if device_ids is not None:
        _sync_device_access(role, device_ids, db)
    db.commit()
    db.refresh(role)
    return _role_response(role, db)


@router.delete("/api/roles/{role_id}")
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(404, detail="角色不存在")
    if role.is_builtin:
        raise HTTPException(400, detail="内置角色不能删除")
    user_count = db.query(User).filter(User.role_id == role_id).count()
    if user_count > 0:
        raise HTTPException(400, detail=f"该角色下有 {user_count} 个用户，请先解除绑定")
    db.delete(role)
    db.commit()
    return {"message": "已删除"}


def _sync_device_access(role: Role, device_ids: list[int], db: Session):
    db.query(RoleDeviceAccess).filter(RoleDeviceAccess.role_id == role.id).delete()
    for did in device_ids:
        db.add(RoleDeviceAccess(role_id=role.id, device_id=did))
    db.commit()


def _role_response(role: Role, db: Session) -> dict:
    user_count = db.query(User).filter(User.role_id == role.id).count()
    try:
        perms = json.loads(role.permissions)
    except (json.JSONDecodeError, TypeError):
        perms = []
    device_ids = [r.device_id for r in role.device_access]
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "permissions": perms,
        "device_scope": role.device_scope,
        "device_ids": device_ids,
        "is_builtin": role.is_builtin,
        "created_at": role.created_at,
        "user_count": user_count,
    }
