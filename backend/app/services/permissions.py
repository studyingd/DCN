"""Permission definitions and dependency factories for RBAC.

权限目录刻意保持精简：8 个键、5 个分组，一个键对应一类可独立授予的能力。
下线的旧键由 ``PERMISSION_MIGRATION`` 描述、迁移 0034 就地改写数据，因此
这里只需按新键判定，不必再兼容历史字符串。

资源授权只有一个「设备范围」（``Role.device_scope``），但它同时管两张 ACL 表：

* ``role_device_access`` —— 物理/云主机
* ``role_pve_guest_access`` —— PVE 虚拟机与 LXC

``all``（默认）表示两类资源都不受限；``selected`` 表示只能访问白名单内的
设备与虚拟机。未勾任何虚拟机时，该角色就看不到虚拟化资源。
"""

import json
from collections.abc import Iterable

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.role_device_access import RoleDeviceAccess
from app.models.role_pve_guest_access import RolePveGuestAccess
from app.models.user import User
from app.services.auth import get_current_user

# guest 的稳定身份：PVE 连接 + 类型(qemu;LXC 已移除,列与三元组契约保留) + vmid。
PveGuestKey = tuple[int, str, int]

PERMISSIONS: dict[str, str] = {
    "device:view": "查看设备",
    "device:manage": "修改设备",
    "device:remote": "远程访问与诊断",
    "pve:view": "查看虚拟机",
    "pve:manage": "管理虚拟机",
    "automation:manage": "运维编排（脚本/巡检/告警）",
    "user:manage": "用户和角色管理",
    "settings:manage": "系统设置",
}

PERMISSION_GROUPS: list[dict] = [
    {"label": "资源查看", "permissions": ["device:view", "pve:view"]},
    {"label": "资源操作", "permissions": ["device:manage", "pve:manage"]},
    {"label": "远程与诊断", "permissions": ["device:remote"]},
    {"label": "运维编排", "permissions": ["automation:manage"]},
    {"label": "系统管理", "permissions": ["user:manage", "settings:manage"]},
]

# 旧键 → 新键（一对多）。迁移 0034 改写库中数据；这里保留同一份映射，
# 用于兜底解析尚未保存过的历史角色行，避免旧 token/旧数据直接失权。
PERMISSION_MIGRATION: dict[str, tuple[str, ...]] = {
    "script:manage": ("automation:manage",),
    "inspection:manage": ("automation:manage",),
    "alert:manage": ("automation:manage",),
    "agent:use": ("device:remote",),
    # 容器控制端点同时服务普通设备与 PVE guest，故拆给两个新键。
    "docker:control": ("device:manage", "pve:manage"),
    "device:read": ("device:view",),
    "room:read": ("device:view",),
    "device:write": ("device:manage",),
    "device:delete": ("device:manage",),
    "room:write": ("device:manage",),
    "device:terminal": ("device:remote",),
}


def _normalize(raw: object) -> list[str]:
    """把角色行里的权限列表映射到当前目录（去重、保序）。"""
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for key in raw:
        if not isinstance(key, str):
            continue
        for candidate in PERMISSION_MIGRATION.get(key, (key,)):
            if candidate in PERMISSIONS and candidate not in result:
                result.append(candidate)
    return result


def get_user_permissions(user: User, db: Session) -> list[str]:
    if user.role_ref is None:
        return []
    try:
        raw = json.loads(user.role_ref.permissions)
    except (json.JSONDecodeError, TypeError):
        return []
    # 已下线的键（例如曾经的全局凭据管理权限）会被丢弃或改写，
    # 保证 /profile 与 token 里的权限集合始终等于当前目录的子集。
    return _normalize(raw)


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


def get_user_pve_guest_keys(user: User, db: Session) -> set[PveGuestKey] | None:
    """Return None if every guest is accessible, else the granted guest set.

    与设备共用同一个「设备范围」：``device_scope='all'`` 时虚拟机不受限，
    ``'selected'`` 时只放行 ``role_pve_guest_access`` 白名单内的 guest。
    """
    if user.role_ref is None:
        return set()
    if user.role_ref.device_scope != "selected":
        return None
    rows = (
        db.query(
            RolePveGuestAccess.connection_id,
            RolePveGuestAccess.guest_type,
            RolePveGuestAccess.vmid,
        )
        .filter(RolePveGuestAccess.role_id == user.role_ref.id)
        .all()
    )
    return {(r[0], r[1], r[2]) for r in rows}


def user_has_permission(user: User, permission: str, db: Session) -> bool:
    # System administrators (Role.is_admin) implicitly hold every permission.
    # This is driven by a dedicated DB flag, NOT a free-text role name, so it
    # cannot be triggered by renaming a role to a magic string.
    if user.role_ref is not None and user.role_ref.is_admin:
        return True
    perms = get_user_permissions(user, db)
    return permission in perms


def user_has_any_permission(
    user: User, permissions: Iterable[str], db: Session
) -> bool:
    if user.role_ref is not None and user.role_ref.is_admin:
        return True
    held = set(get_user_permissions(user, db))
    return any(permission in held for permission in permissions)


def user_can_access_device(user: User, device_id: int, db: Session) -> bool:
    allowed = get_user_device_ids(user, db)
    if allowed is None:
        return True
    return device_id in allowed


def user_can_access_pve_guest(
    user: User,
    db: Session,
    connection_id: int,
    guest_type: str,
    vmid: int,
) -> bool:
    """虚拟机级 ACL 判定；``device_scope='all'`` 时恒为 True。"""
    allowed = get_user_pve_guest_keys(user, db)
    if allowed is None:
        return True
    return (connection_id, guest_type, vmid) in allowed


def user_can_access_pve_vmid(
    user: User, db: Session, connection_id: int, vmid: int
) -> bool:
    """仅凭 ``connection_id + vmid`` 判定（合成 target_id 不携带 guest_type）。

    PVE 的 vmid 在集群内唯一，因此忽略 guest_type 不会误匹配。
    """
    allowed = get_user_pve_guest_keys(user, db)
    if allowed is None:
        return True
    return any(key[0] == connection_id and key[2] == vmid for key in allowed)


def filter_pve_guests(
    user: User,
    db: Session,
    guests: Iterable[tuple],
    *,
    connection_attr: int = 0,
    type_attr: int = 1,
    vmid_attr: int = 2,
) -> list[tuple]:
    """按角色 ACL 过滤 ``(connection_id, guest_type, vmid, ...)`` 序列。"""
    allowed = get_user_pve_guest_keys(user, db)
    if allowed is None:
        return list(guests)
    return [
        row
        for row in guests
        if (row[connection_attr], row[type_attr], row[vmid_attr]) in allowed
    ]


def user_can_access_pve(
    user: User,
    db: Session,
    *,
    manage: bool = False,
    guest: PveGuestKey | None = None,
) -> bool:
    """PVE 是独立资源域，不走设备 ACL 表，但与设备共用「设备范围」开关。

    ``guest`` 为 None 时只判定权限族（用于"是否显示虚拟化入口"这类场景）；
    给定具体 guest 时再叠加虚拟机白名单。系统管理员通过
    ``user_has_permission`` 的 ``is_admin`` 短路隐式放行。
    """
    required = "pve:manage" if manage else "pve:view"
    if not user_has_any_permission(user, (required, "pve:manage"), db):
        return False
    if guest is None:
        return True
    return user_can_access_pve_guest(user, db, *guest)


def require_pve_permission(*, manage: bool = False):
    required = "pve:manage" if manage else "pve:view"

    def _checker(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not current_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用"
            )
        if not user_can_access_pve(current_user, db, manage=manage):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：需要 {required}",
            )
        return current_user

    return _checker


def user_can_access_pve_connection(user: User, db: Session, connection_id: int) -> bool:
    """连接级访问判定：``pve:manage`` 全放行；``pve:view`` 需要该连接上有
    ≥1 台授权虚机。

    连接级读接口(overview/connections 列表)的守卫。虚机级 ACL 只把守
    guest 端点，连接本身的节点/存储等基础设施信息不应泄露给在该连接上
    一台虚机都看不到的用户(越权实测：只授权 2:qemu:103 的账号能读
    连接 1 的全部节点信息)。
    """
    if user_can_access_pve(user, db, manage=True):
        return True
    if not user_can_access_pve(user, db):
        return False
    allowed = get_user_pve_guest_keys(user, db)
    if allowed is None:  # device_scope='all' → 虚拟机不受限
        return True
    return any(key[0] == connection_id for key in allowed)


def require_pve_connection_permission():
    """连接级读端点的依赖工厂(路径参数 conn_id，与 pve 路由同名)。"""

    def _checker(
        conn_id: int,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not current_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用"
            )
        if not user_can_access_pve_connection(current_user, db, conn_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该 PVE 连接"
            )
        return current_user

    return _checker


def require_pve_guest_permission(*, manage: bool = False):
    """针对 ``/connections/{conn_id}/guests/{gtype}/{vmid}/...`` 的依赖工厂。

    路径参数名固定为 conn_id/gtype/vmid，与 pve 路由保持一致。
    """

    def _checker(
        conn_id: int,
        gtype: str,
        vmid: int,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not current_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用"
            )
        if not user_can_access_pve(
            current_user, db, manage=manage, guest=(conn_id, gtype, vmid)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该虚拟机"
            )
        return current_user

    return _checker


def user_can_use_credential(
    user: User, device: Device, credential_id: int | None, db: Session
) -> bool:
    """Check that a supplied credential is authorized for the target device.

    Remote access users may use the device's bound credential, or omit the
    credential id and let the server resolve that binding.  A credential
    manager may still manage secrets, but that must not implicitly grant the
    ability to use a secret against an unrelated device.
    """
    # Credentials are owned by the target device, so device access is the only
    # gate.  The optional ID is kept in older request payloads only for wire
    # compatibility and is ignored.
    return user_can_access_device(user, device.id, db)


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


def require_any_permission(*permissions: str):
    """满足其中任意一个权限即放行（用于跨资源域的同一端点）。"""

    def _checker(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not current_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="账户已被禁用",
            )
        if not user_has_any_permission(current_user, permissions, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user

    return _checker
