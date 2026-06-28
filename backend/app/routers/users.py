from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.user_group import UserGroup
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.schemas.user_group import UserGroupCreate, UserGroupResponse, UserGroupUpdate
from app.services.auth import hash_password
from app.services.permissions import require_permission
from app.utils import apply_update

router = APIRouter(tags=["users"])


# ── User helpers ──────────────────────────────────────────────


def _user_response(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role_ref.name if user.role_ref else (user.role or "viewer"),
        "display_name": user.display_name,
        "is_active": user.is_active,
        "role_id": user.role_id,
        "role_name": user.role_ref.name if user.role_ref else None,
        "group_id": user.group_id,
        "group_name": user.group_ref.name if user.group_ref else None,
        "created_at": user.created_at,
    }


# ── User CRUD ─────────────────────────────────────────────────


@router.post("/api/users", response_model=UserResponse)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, detail="用户名已存在")
    user = User(
        username=body.username,
        password=hash_password(body.password),
        display_name=body.display_name,
        role_id=body.role_id,
        group_id=body.group_id,
        is_active=body.is_active,
    )
    if not user.role:
        user.role = "viewer"
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.get("/api/users", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    users = db.query(User).order_by(User.id).all()
    return [_user_response(u) for u in users]


@router.get("/api/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="用户不存在")
    return _user_response(user)


@router.put("/api/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="用户不存在")
    data = body.model_dump(exclude_unset=True)
    if "password" in data:
        raw = data.pop("password")
        if raw:
            user.password = hash_password(raw)
    apply_update(user, data, ["username", "display_name", "role_id", "group_id", "is_active"])
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.delete("/api/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("user:manage")),
):
    if admin.id == user_id:
        raise HTTPException(400, detail="不能删除自己")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="用户不存在")
    db.delete(user)
    db.commit()
    return {"message": "已删除"}


@router.put("/api/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("user:manage")),
):
    if admin.id == user_id:
        raise HTTPException(400, detail="不能禁用自己")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="用户不存在")
    user.is_active = 0 if user.is_active else 1
    db.commit()
    return {"is_active": user.is_active}


@router.post("/api/users/{user_id}/unlock")
def unlock_user(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    """管理员手动解锁被锁定的账户。"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, detail="用户不存在")
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    return {"message": "账户已解锁"}


# ── User Group CRUD ───────────────────────────────────────────


@router.post("/api/user-groups", response_model=UserGroupResponse)
def create_group(
    body: UserGroupCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    if db.query(UserGroup).filter(UserGroup.name == body.name).first():
        raise HTTPException(400, detail="分组名称已存在")
    group = UserGroup(name=body.name, description=body.description)
    db.add(group)
    db.commit()
    db.refresh(group)
    return _group_response(group, db)


@router.get("/api/user-groups", response_model=list[UserGroupResponse])
def list_groups(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    groups = db.query(UserGroup).order_by(UserGroup.id).all()
    return [_group_response(g, db) for g in groups]


@router.put("/api/user-groups/{group_id}", response_model=UserGroupResponse)
def update_group(
    group_id: int,
    body: UserGroupUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    group = db.query(UserGroup).filter(UserGroup.id == group_id).first()
    if not group:
        raise HTTPException(404, detail="分组不存在")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(group, k, v)
    db.commit()
    db.refresh(group)
    return _group_response(group, db)


@router.delete("/api/user-groups/{group_id}")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_permission("user:manage")),
):
    group = db.query(UserGroup).filter(UserGroup.id == group_id).first()
    if not group:
        raise HTTPException(404, detail="分组不存在")
    db.delete(group)
    db.commit()
    return {"message": "已删除"}


def _group_response(group: UserGroup, db: Session) -> dict:
    count = db.query(User).filter(User.group_id == group.id).count()
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "created_at": group.created_at,
        "user_count": count,
    }
