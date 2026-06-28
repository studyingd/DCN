"""双令牌认证系统测试"""

import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Integer, String, Text, create_engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.config import (
    JWT_ALGORITHM,
    JWT_SECRET,
)
from app.database import Base
from app.services import token_blacklist
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)

# ── 测试用 SQLite 数据库 ──
SQLALCHEMY_DATABASE_URL = "sqlite:///test_dual_token.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestUser(Base):
    __tablename__ = "test_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="viewer")
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


Base.metadata.create_all(bind=engine)


def get_test_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _create_test_user(db: Session, username: str = "testuser") -> TestUser:
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    user = TestUser(
        username=username,
        password=pwd_context.hash("Test@1234"),
        role="viewer",
        is_active=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── 单元测试 ──


def test_access_token_has_correct_claims():
    """Access token 包含正确的 claims"""
    token = create_access_token(
        user_id=1,
        username="admin",
        role="admin",
        permissions=["device:view"],
        device_scope="all",
    )
    payload = decode_token(token)
    assert payload["sub"] == "1"
    assert payload["username"] == "admin"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert "jti" in payload
    assert "exp" in payload


def test_refresh_token_has_correct_claims():
    """Refresh token 包含正确的 claims"""
    token, jti = create_refresh_token(user_id=42)
    payload = decode_token(token)
    assert payload["sub"] == "42"
    assert payload["type"] == "refresh"
    assert payload["jti"] == jti
    assert "exp" in payload


def test_access_and_refresh_tokens_are_different_types():
    """Access 和 refresh token 的 type 不同"""
    access = create_access_token(user_id=1, username="a", role="r")
    refresh, _ = create_refresh_token(user_id=1)
    access_payload = decode_token(access)
    refresh_payload = decode_token(refresh)
    assert access_payload["type"] == "access"
    assert refresh_payload["type"] == "refresh"


def test_token_blacklist_add_and_check():
    """黑名单添加和查询"""
    db = next(get_test_db())
    jti = "test-jti-" + str(int(time.time()))
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    token_blacklist.add_to_blacklist(jti, 1, "test", expires, db)
    assert token_blacklist.is_blacklisted(jti, db) is True
    assert token_blacklist.is_blacklisted("nonexistent-jti", db) is False


def test_token_blacklist_expired_is_not_blacklisted():
    """过期的黑名单条目应不再阻止"""
    db = next(get_test_db())
    jti = "expired-jti-" + str(int(time.time()))
    # Already expired
    expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    token_blacklist.add_to_blacklist(jti, 1, "test", expires, db)
    assert token_blacklist.is_blacklisted(jti, db) is False


def test_token_rotation():
    """刷新后旧 refresh token 应被黑名单"""
    db = next(get_test_db())
    token, jti = create_refresh_token(user_id=1)
    payload = decode_token(token)
    old_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

    # 模拟 rotation：将旧 token 加入黑名单
    token_blacklist.add_to_blacklist(jti, 1, "token-refresh", old_exp, db)
    assert token_blacklist.is_blacklisted(jti, db) is True


def test_expired_access_token_raises():
    """过期的 access token 解码应失败"""
    expired_token = jwt.encode(
        {
            "sub": "1",
            "type": "access",
            "jti": "x",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(expired_token)


def test_invalid_token_raises():
    """无效 token 解码应失败"""
    with pytest.raises(jwt.PyJWTError):
        decode_token("invalid.token.here")


# ── API 集成测试 ──


def _setup_test_app():
    """创建带完整认证流程的测试应用"""
    app = FastAPI()

    @app.post("/api/auth/login")
    def test_login(body: dict, db: Session = Depends(get_test_db)):
        user = (
            db.query(TestUser).filter(TestUser.username == body.get("username")).first()
        )
        if not user:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        access = create_access_token(
            user_id=user.id, username=user.username, role=user.role
        )
        refresh, _ = create_refresh_token(user.id)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
        }

    @app.post("/api/auth/refresh")
    def test_refresh(body: dict, db: Session = Depends(get_test_db)):
        rt = body.get("refresh_token")
        if not rt:
            raise HTTPException(status_code=400, detail="缺少 refresh_token")
        try:
            payload = decode_token(rt)
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Refresh token 已过期")
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="无效的 refresh token")

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="无效的令牌类型")

        old_jti = payload.get("jti", "")
        if old_jti and token_blacklist.is_blacklisted(old_jti, db):
            raise HTTPException(status_code=401, detail="Refresh token 已失效")

        user_id = int(payload.get("sub"))
        user = db.query(TestUser).filter(TestUser.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")

        old_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        if old_jti:
            token_blacklist.add_to_blacklist(
                old_jti, user.id, "token-refresh", old_exp, db
            )

        new_access = create_access_token(
            user_id=user.id, username=user.username, role=user.role
        )
        new_refresh, _ = create_refresh_token(user.id)
        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": "bearer",
        }

    @app.get("/api/protected")
    def protected(user: TestUser = Depends(get_current_user)):
        return {"user_id": user.id, "username": user.username}

    return app


def test_login_returns_both_tokens():
    """登录返回 access_token 和 refresh_token"""
    app = _setup_test_app()
    client = TestClient(app)

    # Create test user
    db = next(get_test_db())
    user = _create_test_user(db, "dualtoken_user")
    try:
        resp = client.post(
            "/api/auth/login",
            json={"username": "dualtoken_user", "password": "Test@1234"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
    finally:
        db.query(TestUser).filter(TestUser.username == "dualtoken_user").delete()
        db.commit()


def test_refresh_returns_new_token_pair():
    """刷新 token 返回新的 token 对"""
    app = _setup_test_app()
    client = TestClient(app)

    db = next(get_test_db())
    user = _create_test_user(db, "refresh_test_user")
    try:
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "refresh_test_user", "password": "Test@1234"},
        )
        tokens = login_resp.json()

        refresh_resp = client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_resp.status_code == 200
        new_tokens = refresh_resp.json()
        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        assert new_tokens["access_token"] != tokens["access_token"]
        assert new_tokens["refresh_token"] != tokens["refresh_token"]
    finally:
        db.query(TestUser).filter(TestUser.username == "refresh_test_user").delete()
        db.commit()


def test_old_refresh_token_rejected_after_rotation():
    """Rotation 后旧的 refresh token 被拒绝"""
    app = _setup_test_app()
    client = TestClient(app)

    db = next(get_test_db())
    user = _create_test_user(db, "rotation_test_user")
    try:
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "rotation_test_user", "password": "Test@1234"},
        )
        tokens = login_resp.json()
        old_refresh = tokens["refresh_token"]

        # First refresh succeeds
        client.post("/api/auth/refresh", json={"refresh_token": old_refresh})

        # Second use of the same refresh token should fail
        resp = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
        assert resp.status_code == 401
        assert "已失效" in resp.json()["detail"]
    finally:
        db.query(TestUser).filter(TestUser.username == "rotation_test_user").delete()
        db.commit()


def test_access_token_cannot_be_used_as_refresh():
    """Access token 不能用于刷新"""
    app = _setup_test_app()
    client = TestClient(app)

    db = next(get_test_db())
    user = _create_test_user(db, "type_check_user")
    try:
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "type_check_user", "password": "Test@1234"},
        )
        tokens = login_resp.json()

        resp = client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["access_token"]}
        )
        assert resp.status_code == 401
    finally:
        db.query(TestUser).filter(TestUser.username == "type_check_user").delete()
        db.commit()
