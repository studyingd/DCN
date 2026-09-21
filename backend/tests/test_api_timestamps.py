"""出站时间戳必须自带时区偏移。

MySQL 的 ``DATETIME`` 不保存时区，连接又固定在 ``+00:00``，所以库里存的其实是 UTC
墙钟时间。以前 ORM 读回来是 naive 值，一路序列化成 ``2026-09-08T09:02:15``，前端
``new Date()`` 会按浏览器本地时区解析，展示出来慢 8 小时(飞书卡片同理)。
``UTCDateTime`` 让读出的值就是 aware UTC，接口自然带上偏移。
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.role import Role
from app.models.user import User
from app.services.auth import create_access_token

client = TestClient(app)

_HAS_OFFSET = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _role(db, name: str, **extra) -> Role:
    role = Role(
        name=name, permissions=json.dumps(["user:manage"]), is_admin=True, **extra
    )
    db.add(role)
    db.commit()
    return role


def test_orm_reads_back_aware_utc(db):
    stamp = datetime.now(timezone.utc).timestamp()
    role = _role(db, f"tz_role_{stamp}")
    try:
        db.expire(role)  # 逼着从库里重新读，而不是用内存里的对象
        assert role.created_at is not None
        assert role.created_at.utcoffset() == timedelta(0)
    finally:
        db.delete(role)
        db.commit()


def test_written_timestamps_are_normalized_to_utc(db):
    """naive 按 UTC 解释、带偏移的先换算，落库都是同一份 UTC 墙钟时间。"""
    stamp = datetime.now(timezone.utc).timestamp()
    expected = datetime(2026, 9, 8, 9, 2, 15, tzinfo=timezone.utc)
    cases = {
        "naive": datetime(2026, 9, 8, 9, 2, 15),
        "shanghai": datetime(
            2026, 9, 8, 17, 2, 15, tzinfo=timezone(timedelta(hours=8))
        ),
    }
    for label, value in cases.items():
        role = _role(db, f"tz_{label}_{stamp}", created_at=value)
        try:
            db.expire(role)
            assert role.created_at == expected, label
        finally:
            db.delete(role)
            db.commit()


def test_api_response_timestamps_carry_offset(db):
    """接口返回的时间字符串要能被 new Date() 无歧义地解析。"""
    stamp = datetime.now(timezone.utc).timestamp()
    role = _role(db, f"tz_api_{stamp}")
    user = User(
        username=f"tzuser_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["user:manage"],
        device_scope="all",
    )
    try:
        res = client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        stamps = [row["created_at"] for row in res.json() if row.get("created_at")]
        assert stamps, "响应里没有时间字段，用例失去意义"
        assert all(_HAS_OFFSET.search(value) for value in stamps), stamps[:3]
    finally:
        db.delete(user)
        db.delete(role)
        db.commit()
