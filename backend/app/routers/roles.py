import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.role import Role
from app.models.role_device_access import RoleDeviceAccess
from app.models.role_pve_guest_access import RolePveGuestAccess
from app.models.user import User
from app.schemas.business import PveGuestCandidate
from app.schemas.role import PveGuestRef, RoleCreate, RoleResponse, RoleUpdate
from app.services.permissions import (
    PERMISSION_GROUPS,
    PERMISSIONS,
    require_permission,
)
from app.services.pve_guest_candidates import collect_pve_guest_candidates
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
    _sync_pve_guest_access(role, body.pve_guests, db)
    return _role_response(role, db)


@router.get("/api/roles", response_model=list[RoleResponse])
def list_roles(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    # device_access 惰性加载 + 逐角色 count 会让这里变成 1+2N 条查询，
    # 改成一次预加载 + 一次 GROUP BY。
    roles = (
        db.query(Role)
        .options(selectinload(Role.device_access), selectinload(Role.pve_guest_access))
        .order_by(Role.id)
        .all()
    )
    return [_role_response(r, db, _user_counts_by_role(db)) for r in roles]


@router.get(
    "/api/roles/pve-guest-candidates",
    response_model=list[PveGuestCandidate],
)
def list_role_pve_guest_candidates(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    """角色授权用的虚拟机候选项。

    必须声明在 ``/api/roles/{role_id}`` 之前，否则会被路径参数吞掉。
    这里故意不叠加调用者自身的虚拟机 ACL：能编辑角色的人需要看到全量
    guest 才能完成授权。
    """
    return [PveGuestCandidate(**item) for item in collect_pve_guest_candidates(db)]


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
    pve_guests = data.pop("pve_guests", None)
    # is_admin is intentionally NOT settable here — it is a system flag managed
    # only via DB/migration, never via the role update API.
    apply_update(
        role,
        data,
        ["name", "description", "permissions", "device_scope"],
    )
    if device_ids is not None:
        _sync_device_access(role, device_ids, db)
    if pve_guests is not None:
        _sync_pve_guest_access(role, pve_guests, db)
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


def _sync_pve_guest_access(
    role: Role, pve_guests: list[PveGuestRef] | list[dict], db: Session
):
    """重建角色的虚拟机白名单；名称快照用于 PVE 不可达时仍能显示。"""
    db.query(RolePveGuestAccess).filter(RolePveGuestAccess.role_id == role.id).delete()
    seen: set[tuple[int, str, int]] = set()
    for item in pve_guests:
        data = item.model_dump() if isinstance(item, PveGuestRef) else dict(item)
        key = (data["connection_id"], data["guest_type"], data["vmid"])
        if key in seen:
            continue
        seen.add(key)
        db.add(
            RolePveGuestAccess(
                role_id=role.id,
                connection_id=key[0],
                guest_type=key[1],
                vmid=key[2],
                guest_name=data.get("guest_name"),
            )
        )
    db.commit()


def _user_counts_by_role(db: Session) -> dict[int, int]:
    """一次取回每个角色的用户数，供列表接口批量使用。"""
    return dict(
        db.query(User.role_id, func.count(User.id))
        .filter(User.role_id.isnot(None))
        .group_by(User.role_id)
        .all()
    )


def _role_response(
    role: Role, db: Session, user_counts: dict[int, int] | None = None
) -> dict:
    # 列表接口传批量统计结果；单条查询才现算，避免多一次 GROUP BY。
    user_count = (
        user_counts.get(role.id, 0)
        if user_counts is not None
        else db.query(User).filter(User.role_id == role.id).count()
    )
    try:
        perms = json.loads(role.permissions)
    except (json.JSONDecodeError, TypeError):
        perms = []
    device_ids = [r.device_id for r in role.device_access]
    # 虚拟机与设备共用「设备范围」：device_scope='all' 时白名单不参与判定，
    # 但仍原样回读，便于切回 selected 时保留上次勾选。
    pve_guests = [
        {
            "connection_id": r.connection_id,
            "guest_type": r.guest_type,
            "vmid": r.vmid,
            "guest_name": r.guest_name,
        }
        for r in role.pve_guest_access
    ]
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "permissions": perms,
        "device_scope": role.device_scope,
        "device_ids": device_ids,
        "pve_guests": pve_guests,
        "is_builtin": role.is_builtin,
        "created_at": role.created_at,
        "user_count": user_count,
    }
