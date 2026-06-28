"""Permission definitions and dependency factories for RBAC."""

import json

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.role_device_access import RoleDeviceAccess
from app.models.user import User
from app.services.auth import get_current_user

PERMISSIONS: dict[str, str] = {
    "device:view": "查看设备",
    "device:manage": "修改设备",
    "device:remote": "远程访问",
    "script:manage": "脚本管理",
    "credential:manage": "凭据管理",
    "user:manage": "用户和角色管理",
    "audit:manage": "审计管理",
    "inspection:manage": "巡检管理",
}

PERMISSION_GROUPS: list[dict] = [
    {"label": "权限", "permissions": list(PERMISSIONS.keys())},
]

# Mapping from old permission keys to new keys for data migration
PERMISSION_MIGRATION: dict[str, str] = {
    "device:read": "device:view",
    "room:read": "device:view",
    "device:write": "device:manage",
    "device:delete": "device:manage",
    "room:write": "device:manage",
    "device:terminal": "device:remote",
    "credential:read": "credential:manage",
    "credential:write": "credential:manage",
    "audit:read": "audit:manage",
    "settings:manage": "audit:manage",
    # user:manage stays the same
}


def get_user_permissions(user: User, db: Session) -> list[str]:
    if user.role_ref is None:
        return []
    try:
        return json.loads(user.role_ref.permissions)
    except (json.JSONDecodeError, TypeError):
        return []


def get_user_device_ids(user: User, db: Session) -> list[int] | None:
    """Return None if all devices accessible, or a list of device IDs."""
    if user.role_ref is None:
        return []
    if user.role_ref.device_scope == "all":
        return None
    rows = (
        db.query(RoleDeviceAccess.device_id)
        .filter(RoleDeviceAccess.role_id == user.role_ref.id)
        .all()
    )
    return [r[0] for r in rows]


def user_has_permission(user: User, permission: str, db: Session) -> bool:
    # System administrators (Role.is_admin) implicitly hold every permission.
    # This is driven by a dedicated DB flag, NOT a free-text role name, so it
    # cannot be triggered by renaming a role to a magic string.
    if user.role_ref is not None and user.role_ref.is_admin:
        return True
    perms = get_user_permissions(user, db)
    return permission in perms


def user_can_access_device(user: User, device_id: int, db: Session) -> bool:
    allowed = get_user_device_ids(user, db)
    if allowed is None:
        return True
    return device_id in allowed


def is_admin_user(user: User) -> bool:
    """Return True if the user belongs to a system administrator role.

    Based on the dedicated ``Role.is_admin`` flag rather than the (mutable,
    locale-coupled) role display name.
    """
    return bool(user.role_ref is not None and user.role_ref.is_admin)


def require_permission(permission: str):
    """FastAPI dependency factory: checks that the current user has the given permission."""

    def _checker(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not current_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="账户已被禁用",
            )
        if not user_has_permission(current_user, permission, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user

    return _checker
