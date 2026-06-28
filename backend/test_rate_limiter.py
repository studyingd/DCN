"""速率限制测试"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse


def create_test_app() -> tuple[FastAPI, Limiter]:
    app = FastAPI()
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=["100/minute"],
        storage_uri="memory://",
    )
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "请求过于频繁，请稍后再试",
            },
        )

    app.add_middleware(SlowAPIMiddleware)
    return app, limiter


def test_under_limit_succeeds():
    """未超限请求正常通过"""
    app, limiter = create_test_app()

    @app.get("/limited")
    @limiter.limit("3/minute")
    def limited_route(request: Request):
        return {"ok": True}

    client = TestClient(app)
    for i in range(3):
        resp = client.get("/limited")
        assert resp.status_code == 200, f"Request {i + 1} should succeed"


def test_over_limit_returns_429():
    """超限请求返回 429"""
    app, limiter = create_test_app()

    @app.get("/limited")
    @limiter.limit("3/minute")
    def limited_route(request: Request):
        return {"ok": True}

    client = TestClient(app)
    # Use up the limit
    for _ in range(3):
        client.get("/limited")
    # Next request should be 429
    resp = client.get("/limited")
    assert resp.status_code == 429
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "RATE_LIMIT_EXCEEDED"
    assert "频繁" in data["message"]


def test_unlimited_routes_succeed():
    """无特殊限制的路由使用默认限制（100/分钟），正常请求通过"""
    app, limiter = create_test_app()

    @app.get("/normal")
    def normal_route(request: Request):
        return {"ok": True}

    client = TestClient(app)
    for _ in range(10):
        resp = client.get("/normal")
        assert resp.status_code == 200


def test_login_style_limit():
    """模拟登录接口的严格限制 (5/15minute)"""
    app, limiter = create_test_app()

    @app.post("/login")
    @limiter.limit("5/15minute")
    def login_route(request: Request):
        return {"token": "xxx"}

    client = TestClient(app)
    # 5 requests should succeed
    for i in range(5):
        resp = client.post("/login")
        assert resp.status_code == 200, f"Login attempt {i + 1} should succeed"
    # 6th should fail
    resp = client.post("/login")
    assert resp.status_code == 429


def test_rate_limit_error_format():
    """429 响应格式验证"""
    app, limiter = create_test_app()

    @app.get("/tight")
    @limiter.limit("1/minute")
    def tight_route(request: Request):
        return {"ok": True}

    client = TestClient(app)
    client.get("/tight")  # use up the limit
    resp = client.get("/tight")  # should be 429
    data = resp.json()
    assert "success" in data
    assert "code" in data
    assert "message" in data
    assert data["success"] is False
