import threading
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import COOKIE_SECURE, JWT_EXPIRE_HOURS, REFRESH_TOKEN_EXPIRE_DAYS
from app.database import get_db
from app.middleware.rate_limiter import limiter
from app.models.user import User
from app.schemas.user import TokenResponse, UserLogin, UserResponse
from app.services import token_blacklist
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.services.online_users import get_online_users, record_heartbeat
from app.services.permissions import (
    get_user_device_ids,
    get_user_permissions,
    get_user_pve_guest_keys,
    is_admin_user,
    require_permission,
)
from app.utils import as_utc_aware

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── Brute force protection constants ──
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 30
DUMMY_HASH = "$2b$12$LJ3m4ys3Lk0TSwHCpNqrDObVYaVlF4FlqGDiQhA0Wx1xq0Bx0x0x0"

# ── 不存在用户名的影子锁定 ──
# 越权实测(两轮):真实账户第 5 次失败 429「账户已被锁定」,不存在的用户名
# 永远 401 ——消息差异可直接枚举账户。这里给不存在的用户名做同一套失败
# 计数,阈值/时长/文案与真实账户的 DB 锁定**完全同构**：
#   第 1~4 次 401「用户名或密码错误」；第 5 次 429「密码错误次数过多…」；
#   之后 429「账户已被锁定…」；锁过期后计数不归零(下次失败立即重锁,
#   与真实账户的 failed_login_attempts 不过期同口径)。
# 仅进程内存(限流桶同为内存,单实例部署够用,多实例时再挪 redis);
# key 归一化对齐 MySQL utf8mb4_unicode_ci 的大小写不敏感 + PAD SPACE
# 尾随空格不敏感,否则 Admin/admin / "admin " 会被当成不同名字绕开计数。
_SHADOW_PURGE_SIZE = 512
_SHADOW_STALE_AFTER = timedelta(hours=24)

_shadow_locks: dict[str, tuple[int, datetime | None, datetime]] = {}
_shadow_lock_guard = threading.Lock()


def _shadow_key(username: str) -> str:
    return username.strip().lower()


def _purge_stale_shadow_locks(now: datetime) -> None:
    """字典超界时清掉 24h 未再探测的条目，防无限增长。

    代价：假用户名静置 24h 后计数归零(需重新 5 次才锁)，与真实账户的
    计数永不过期存在极端时序下的理论信号差；内存有界优先，多 IP 洪泛
    场景下这是必要的。"""
    if len(_shadow_locks) <= _SHADOW_PURGE_SIZE:
        return
    stale = [
        key
        for key, (_count, _locked_until, last_seen) in _shadow_locks.items()
        if last_seen + _SHADOW_STALE_AFTER < now
    ]
    for key in stale:
        _shadow_locks.pop(key, None)


# ── Auth cookie configuration ──
_ACCESS_COOKIE = "dcn_access"
_REFRESH_COOKIE = "dcn_refresh"
_COOKIE_KWARGS = {
    "httponly": True,
    "secure": COOKIE_SECURE,
    "samesite": "lax",
    "path": "/",
}


def _set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    """Set httpOnly auth cookies. Tokens are never placed in the JSON body —
    the only credential channel is the cookie, so injected JS cannot read them."""
    response.set_cookie(
        _ACCESS_COOKIE, access, max_age=JWT_EXPIRE_HOURS * 3600, **_COOKIE_KWARGS
    )
    response.set_cookie(
        _REFRESH_COOKIE,
        refresh,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **_COOKIE_KWARGS,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(_ACCESS_COOKIE, path="/")
    response.delete_cookie(_REFRESH_COOKIE, path="/")


def _access_token_from_request(request: Request) -> str | None:
    token = request.cookies.get(_ACCESS_COOKIE)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    return token


def _blacklist_access_token(request: Request, user_id: int, db: Session) -> None:
    """Blacklist the current access token (by jti) if present and not expired."""
    token = _access_token_from_request(request)
    if not token:
        return
    try:
        payload = decode_token(token)
    except (pyjwt.PyJWTError, ValueError):
        return
    jti = payload.get("jti", "")
    exp = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
    if jti:
        try:
            token_blacklist.add_to_blacklist(jti, user_id, "user-action", exp, db)
        except Exception:
            pass


def _pve_guest_tokens(user: User, db: Session) -> list[str]:
    """把授权的虚拟机编码成 ``conn:gtype:vmid``，供前端做本地门禁提示。

    ``device_scope='all'`` 时返回空列表——空列表配合 scope 字段表示"不受限"，
    与 ``device_ids`` 的语义保持一致。
    """
    keys = get_user_pve_guest_keys(user, db)
    if keys is None:
        return []
    return [f"{conn_id}:{gtype}:{vmid}" for conn_id, gtype, vmid in sorted(keys)]


def _make_token_response(
    user: User, db: Session, must_change: bool = False
) -> TokenResponse:
    """Build token response for a successfully authenticated user."""
    perms = get_user_permissions(user, db)
    dev_ids = get_user_device_ids(user, db)
    role_name = user.role_ref.name if user.role_ref else (user.role or "viewer")
    scope = user.role_ref.device_scope if user.role_ref else "all"
    guests = _pve_guest_tokens(user, db)

    access_token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=role_name,
        permissions=perms,
        device_scope=scope,
        device_ids=dev_ids if dev_ids is not None else [],
        pve_guests=guests,
        session_version=getattr(user, "session_version", 0),
    )
    refresh_token_str, _ = create_refresh_token(
        user.id, getattr(user, "session_version", 0)
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        must_change_password=must_change,
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(
    body: UserLogin, request: Request, response: Response, db: Session = Depends(get_db)
):
    """用户登录 — 含暴力破解防护（5 次失败锁定 30 分钟）。

    IP 级限流 10 次/分钟(未认证时限流 key 退化为来源 IP)：叠加账户锁定后，
    对不存在用户名的喷洒/枚举也被压住——账户锁定只对真实账户计数，
    假用户名曾可无限试探(越权审计实测 8 连发无限制)。"""
    user = db.query(User).filter(User.username == body.username).first()

    # ── Constant-time dummy check (prevents username enumeration) ──
    if user is None:
        # 先做 dummy 校验(与存在用户相同的 bcrypt 计时)，再走影子锁定判定
        verify_password(body.password, DUMMY_HASH)
        key = _shadow_key(body.username)
        now = datetime.now(timezone.utc)
        with _shadow_lock_guard:
            _purge_stale_shadow_locks(now)
            count, locked_until, _last_seen = _shadow_locks.get(key, (0, None, now))
            locked_until = (
                as_utc_aware(locked_until) if locked_until is not None else None
            )
            if locked_until is not None and locked_until > now:
                remaining_sec = (locked_until - now).total_seconds()
                remaining_min = max(1, int(remaining_sec / 60))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"账户已被锁定，请在 {remaining_min} 分钟后重试",
                )
            count += 1
            if count >= MAX_FAILED_ATTEMPTS:
                _shadow_locks[key] = (
                    count,
                    now + timedelta(minutes=LOCKOUT_DURATION_MINUTES),
                    now,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"密码错误次数过多，账户已被锁定 {LOCKOUT_DURATION_MINUTES} 分钟",
                )
            _shadow_locks[key] = (count, locked_until, now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # ── Account lockout check ──
    if user.is_locked:
        locked_until = as_utc_aware(user.locked_until)
        remaining_sec = (
            (locked_until or datetime.now(timezone.utc)) - datetime.now(timezone.utc)
        ).total_seconds()
        remaining_min = max(1, int(remaining_sec / 60))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"账户已被锁定，请在 {remaining_min} 分钟后重试",
        )

    # ── Password verification ──
    if not verify_password(body.password, user.password):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = datetime.now(timezone.utc) + timedelta(
                minutes=LOCKOUT_DURATION_MINUTES
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"密码错误次数过多，账户已被锁定 {LOCKOUT_DURATION_MINUTES} 分钟",
            )
        db.commit()
        # Use same message as non-existent user to prevent enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # ── Inactive check ──
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账户已被禁用",
        )

    # ── Login successful — reset security counters ──
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login = datetime.now(timezone.utc)
    db.commit()

    # ── Online tracking ──
    role_name = user.role_ref.name if user.role_ref else (user.role or "viewer")
    record_heartbeat(user.id, user.username, role_name)

    token_response = _make_token_response(
        user, db, must_change=user.must_change_password
    )
    _set_auth_cookies(
        response, token_response.access_token, token_response.refresh_token
    )
    # 凭证只经 httpOnly Cookie 下发;响应体不携带 token,注入的 JS 无法读取。
    return TokenResponse(
        access_token="",
        refresh_token="",
        must_change_password=token_response.must_change_password,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
def refresh_token_endpoint(
    request: Request,
    response: Response,
    refresh_token: str | None = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    """用 refresh token 换取新的 token 对（Rotation 模式）"""
    # Cookie-first: the browser SPA posts with no body and the refresh token
    # lives in the httpOnly cookie. Fall back to the JSON body for API clients.
    if not refresh_token:
        refresh_token = request.cookies.get(_REFRESH_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="缺少 refresh token")
    # 1. 解码验证
    try:
        payload = decode_token(refresh_token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token 已过期，请重新登录")
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效的 refresh token")

    # 2. 类型检查
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="无效的令牌类型")

    # 3. 黑名单检查
    old_jti = payload.get("jti", "")
    if old_jti and token_blacklist.is_blacklisted(old_jti, db):
        raise HTTPException(status_code=401, detail="Refresh token 已失效")

    # 4. 用户存在性检查
    user_id = int(payload.get("sub"))
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    if int(payload.get("session_version", 0)) != int(
        getattr(user, "session_version", 0)
    ):
        raise HTTPException(status_code=401, detail="Refresh token 已失效")

    # 5. 颁发新 token 对（先发新 token 再黑名单旧 token，避免中间崩溃导致用户被锁定）
    perms = get_user_permissions(user, db)
    dev_ids = get_user_device_ids(user, db)
    role_name = user.role_ref.name if user.role_ref else (user.role or "viewer")
    scope = user.role_ref.device_scope if user.role_ref else "all"
    new_access = create_access_token(
        user_id=user.id,
        username=user.username,
        role=role_name,
        permissions=perms,
        device_scope=scope,
        device_ids=dev_ids if dev_ids is not None else [],
        pve_guests=_pve_guest_tokens(user, db),
        session_version=getattr(user, "session_version", 0),
    )
    new_refresh, _ = create_refresh_token(user.id, getattr(user, "session_version", 0))

    # 6. 旧的 refresh token 加入黑名单（Rotation）— 在新 token 发放之后
    old_exp = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
    if old_jti:
        try:
            token_blacklist.add_to_blacklist(
                old_jti, user.id, "token-refresh", old_exp, db
            )
        except Exception:
            # If blacklisting fails, the old token remains valid for its remaining
            # lifetime — far less disruptive than locking the user out.
            pass

    _set_auth_cookies(response, new_access, new_refresh)
    return TokenResponse(access_token="", refresh_token="")


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    refresh_token: str | None = Body(None, embed=True),
):
    """登出 — 将 access token 和 refresh token 同时加入黑名单，并清除 Cookie"""
    # Access token (from httpOnly cookie or Authorization header)
    _blacklist_access_token(request, current_user.id, db)

    # Refresh token: cookie first, then optional body
    refresh_token = refresh_token or request.cookies.get(_REFRESH_COOKIE)
    if refresh_token:
        try:
            payload = decode_token(refresh_token)
            jti = payload.get("jti", "")
            exp = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
            if jti:
                token_blacklist.add_to_blacklist(
                    jti, current_user.id, "user-logout", exp, db
                )
        except (pyjwt.PyJWTError, ValueError):
            pass

    _clear_auth_cookies(response)
    return {"message": "已成功登出"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    role_name = (
        current_user.role_ref.name
        if current_user.role_ref
        else (current_user.role or "viewer")
    )
    record_heartbeat(current_user.id, current_user.username, role_name)
    return _user_to_response(current_user)


@router.get("/online-users")
def list_online_users(
    _current_user: User = Depends(require_permission("user:manage")),
):
    return get_online_users()


@router.get("/profile")
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户的非敏感画像（权限/设备作用域/角色），供前端驱动 UI。

    浏览器端不再解码 JWT（token 仅存于 httpOnly Cookie），改由本接口获取权限。
    """
    perms = get_user_permissions(current_user, db)
    dev_ids = get_user_device_ids(current_user, db)
    role_name = (
        current_user.role_ref.name
        if current_user.role_ref
        else (current_user.role or "viewer")
    )
    scope = current_user.role_ref.device_scope if current_user.role_ref else "all"
    return {
        "id": current_user.id,
        "username": current_user.username,
        "display_name": current_user.display_name,
        "role": role_name,
        "is_admin": is_admin_user(current_user),
        "permissions": perms,
        "device_scope": scope,
        "device_ids": dev_ids if dev_ids is not None else [],
        "pve_guests": _pve_guest_tokens(current_user, db),
        "must_change_password": current_user.must_change_password,
    }


@router.post("/change-password")
def change_password(
    request: Request,
    response: Response,
    old_password: str = Body(..., embed=True),
    new_password: str = Body(..., embed=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改当前用户密码 — 同时标记为已改密（解除首次登录强制改密）。"""
    if not verify_password(old_password, current_user.password):
        raise HTTPException(status_code=400, detail="原密码错误")
    from app.validators import validate_password_strength

    try:
        validate_password_strength(new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_user.password = hash_password(new_password)
    current_user.password_changed_at = datetime.now(timezone.utc)
    current_user.session_version = int(getattr(current_user, "session_version", 0)) + 1
    db.commit()
    # Invalidate the current access/refresh token pair: the session version was
    # incremented above, and clearing both cookies forces a fresh login.
    _blacklist_access_token(request, current_user.id, db)
    _clear_auth_cookies(response)
    return {"message": "密码修改成功"}


def _user_to_response(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role_ref.name if user.role_ref else (user.role or "viewer"),
        "display_name": user.display_name,
        "is_active": user.is_active,
        "role_id": user.role_id,
        "role_name": user.role_ref.name if user.role_ref else None,
        "created_at": user.created_at,
    }
