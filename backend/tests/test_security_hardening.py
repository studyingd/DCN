"""登录安全加固钉子（越权审计批次 4）。

审计实测三个问题：
  1. Host 头注入——/api/health/ 307 重定向的 Location 从请求 Host 头拼出，
     恶意 Host 会生成 http://evil.example.com/api/health；
  2. 登录无有效 IP 级限速——旧限流 30/15min 太松，8 连发假用户名无限制；
  3. /api/health 匿名泄露 database 状态。

修复口径：redirect_slashes=False（尾斜杠 404）；登录 10/minute（未认证时
key 退化为 IP）；health 未认证只回 {"status": "ok"}。
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_trailing_slash_is_404_not_redirect():
    """尾斜杠一律 404：不再产生 307/Location，Host 注入面消失。"""
    res = client.get("/api/health/", headers={"Host": "evil.example.com"})
    assert res.status_code == 404
    assert "location" not in {k.lower() for k in res.headers}


def test_health_anonymous_has_no_database_detail():
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data == {"status": "ok"}
    assert "database" not in data


def test_health_live_stays_anonymous():
    """liveness 探针保持匿名可用（compose HEALTHCHECK 依赖它）。"""
    res = client.get("/api/health/live")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.order(1)
def test_login_rate_limit_ten_per_ip():
    """同一 IP 第 11 次登录请求必须 429（带 Retry-After）。

    用不存在的用户名:即使账户锁定不计数,IP 级限流也要拦住喷洒/枚举。
    """
    payloads = [
        {"username": f"no-such-user-{i}", "password": "whatever-1A!"} for i in range(11)
    ]
    seen_429 = False
    for i, body in enumerate(payloads):
        res = client.post("/api/auth/login", json=body)
        if res.status_code == 429:
            assert i >= 9, f"第 {i + 1} 次就限流,阈值过紧"
            assert "Retry-After" in res.headers
            seen_429 = True
            break
        assert res.status_code == 401, res.text
    assert seen_429, "11 连发没有任何一次 429 —— 登录 IP 级限流失效"


# ── 影子锁定:不存在的用户名与真实账户同构锁定 ──


@pytest.fixture()
def _reset_login_state():
    """隔离登录相关进程内状态:限流桶 + 影子锁,测试前后都清零。

    不清的话前面用例消耗的 10/minute 配额会让这里的 401 变 429,
    断言直接走样。"""
    from app.middleware.rate_limiter import limiter
    from app.routers import auth as auth_router

    limiter.reset()
    auth_router._shadow_locks.clear()
    yield
    limiter.reset()
    auth_router._shadow_locks.clear()


def test_nonexistent_username_locks_out_after_five_failures(_reset_login_state):
    """假用户名 5 次失败必须锁定,且文案与真实账户一致(消除枚举信号)。"""
    body = {"username": "no-such-shadow-user", "password": "Wrong-Pass1!"}
    codes = []
    messages = []
    for _ in range(6):
        res = client.post("/api/auth/login", json=body)
        codes.append(res.status_code)
        messages.append(res.json().get("detail", ""))
    assert codes[:4] == [401] * 4
    assert all(m == "用户名或密码错误" for m in messages[:4])
    assert codes[4] == 429
    assert messages[4] == "密码错误次数过多，账户已被锁定 30 分钟"
    assert codes[5] == 429
    assert re.match(r"账户已被锁定，请在 \d+ 分钟后重试", messages[5])


def test_shadow_lock_is_case_and_space_insensitive(_reset_login_state):
    """Admin/admin / 尾随空格必须命中同一计数(对齐 MySQL CI+PAD SPACE)。"""
    variants = ["Shadow-User", "shadow-user", " shadow-user ", "SHADOW-USER"]
    for i, name in enumerate(variants):
        res = client.post(
            "/api/auth/login", json={"username": name, "password": "Wrong-Pass1!"}
        )
        # 前 4 个变体共 4 次失败;第 5 次任意形态都应触发锁定
        if i < 3:
            assert res.status_code == 401
    res = client.post(
        "/api/auth/login",
        json={"username": "Shadow_User_X", "password": "Wrong-Pass1!"},
    )
    assert res.status_code == 401  # 别的名字不受牵连
    res = client.post(
        "/api/auth/login",
        json={"username": "shadow-user", "password": "Wrong-Pass1!"},
    )
    assert res.status_code == 429
    assert "账户已被锁定" in res.json()["detail"]


def test_lockout_messages_identical_for_real_and_nonexistent(_reset_login_state):
    """真实账户与假用户名的锁定文案逐字一致——枚举无差异。"""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.middleware.rate_limiter import limiter
    from app.models.user import User
    from app.services.auth import hash_password

    stamp = int(datetime.now(timezone.utc).timestamp())
    real_name = f"lockout_real_{stamp}"
    db = SessionLocal()
    try:
        db.add(
            User(
                username=real_name,
                password=hash_password("Correct-Pass1!"),
                role="admin",
                is_active=1,
            )
        )
        db.commit()

        def _fail(username: str) -> tuple[int, str]:
            res = client.post(
                "/api/auth/login",
                json={"username": username, "password": "Wrong-Pass1!"},
            )
            return res.status_code, res.json().get("detail", "")

        real = [_fail(real_name) for _ in range(6)]
        # 12 连发会先触发 10/minute 的 IP 限流(那不是本用例要测的东西),
        # 两组序列之间清一次限流桶;锁定计数器(DB/影子)不受影响。
        limiter.reset()
        fake_name = f"lockout_fake_{stamp}"
        fake = [_fail(fake_name) for _ in range(6)]

        # 两种名字的 (状态码, 文案) 序列必须完全一致
        assert real == fake
        assert [c for c, _m in real] == [401] * 4 + [429] * 2
    finally:
        db.rollback()
        db.query(User).filter(User.username == real_name).delete()
        db.commit()
        db.close()
