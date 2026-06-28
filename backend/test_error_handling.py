"""统一错误处理体系测试"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.exceptions import (
    AppError,
    AuthError,
    BusinessError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    TokenExpiredError,
)
from app.middleware.error_handler import (
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.middleware.trace import TraceMiddleware

# ── 创建测试用 FastAPI 应用 ──
app = FastAPI()
app.add_middleware(TraceMiddleware)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


class SampleBody(BaseModel):
    name: str = Field(min_length=3)
    age: int = Field(ge=0, le=200)


@app.get("/ok")
def ok_route():
    return {"status": "ok"}


@app.get("/not-found")
def not_found_route():
    raise NotFoundError("设备")


@app.get("/auth-error")
def auth_error_route():
    raise AuthError("用户名或密码错误")


@app.get("/forbidden")
def forbidden_route():
    raise ForbiddenError("权限不足")


@app.get("/token-expired")
def token_expired_route():
    raise TokenExpiredError()


@app.get("/conflict")
def conflict_route():
    raise ConflictError("用户名已存在")


@app.get("/rate-limit")
def rate_limit_route():
    raise RateLimitError()


@app.get("/business")
def business_route():
    raise BusinessError("设备正在运行中，无法删除")


@app.post("/validate")
def validate_route(body: SampleBody):
    return {"name": body.name, "age": body.age}


@app.get("/unhandled")
def unhandled_route():
    x = 1 / 0  # noqa: B018 — intentionally trigger division by zero


@app.get("/custom-trace")
def custom_trace_route():
    # Test that X-Trace-Id from request is echoed
    return {"ok": True}


client = TestClient(app)
# For unhandled exceptions, we need a client that doesn't re-raise
# server exceptions, because FastAPI's ServerErrorMiddleware re-raises
# unhandled errors before our generic Exception handler can produce a response.
client_no_raise = TestClient(app, raise_server_exceptions=False)


# ── Test Cases ──


def test_ok_response_has_trace_id():
    """正常响应也携带 X-Trace-Id"""
    resp = client.get("/ok")
    assert resp.status_code == 200
    assert "X-Trace-Id" in resp.headers
    assert len(resp.headers["X-Trace-Id"]) == 36  # UUID format


def test_custom_trace_id_is_echoed():
    """客户端传入的 X-Trace-Id 应原样返回"""
    custom_id = "my-custom-trace-id-1234"
    resp = client.get("/custom-trace", headers={"X-Trace-Id": custom_id})
    assert resp.headers["X-Trace-Id"] == custom_id


def test_not_found_error():
    """NotFoundError → 404 + 统一 JSON"""
    resp = client.get("/not-found")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "NOT_FOUND"
    assert "设备不存在" in data["message"]
    assert "traceId" in data


def test_auth_error():
    """AuthError → 401"""
    resp = client.get("/auth-error")
    assert resp.status_code == 401
    data = resp.json()
    assert data["code"] == "AUTH_ERROR"
    assert "用户名或密码错误" in data["message"]


def test_forbidden_error():
    """ForbiddenError → 403"""
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


def test_token_expired_error():
    """TokenExpiredError → 401"""
    resp = client.get("/token-expired")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_EXPIRED"


def test_conflict_error():
    """ConflictError → 409"""
    resp = client.get("/conflict")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


def test_rate_limit_error():
    """RateLimitError → 429"""
    resp = client.get("/rate-limit")
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMIT_EXCEEDED"


def test_business_error():
    """BusinessError → 400"""
    resp = client.get("/business")
    assert resp.status_code == 400
    assert "设备正在运行" in resp.json()["message"]


def test_validation_error():
    """Pydantic 校验错误 → 422 + 中文消息"""
    resp = client.post("/validate", json={"name": "ab", "age": -1})
    assert resp.status_code == 422
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "VALIDATION_ERROR"
    assert "请求参数验证失败" in data["message"]
    assert "traceId" in data


def test_validation_error_valid_body():
    """合法请求体应通过校验"""
    resp = client.post("/validate", json={"name": "test", "age": 25})
    assert resp.status_code == 200


def test_unhandled_error():
    """未捕获异常 → 500 + 通用消息（不暴露堆栈）"""
    resp = client_no_raise.get("/unhandled")
    assert resp.status_code == 500
    data = resp.json()
    assert data["success"] is False
    assert data["code"] == "SERVER_ERROR"
    assert data["message"] == "服务器内部错误，请稍后重试"
    assert "traceId" in data
    assert "stack" not in data  # 不暴露堆栈


def test_error_response_format_consistency():
    """所有错误响应格式一致：success, code, message, traceId"""
    routes = [
        "/not-found",
        "/auth-error",
        "/forbidden",
        "/conflict",
        "/rate-limit",
        "/business",
    ]
    for route in routes:
        resp = client.get(route)
        data = resp.json()
        assert "success" in data, f"{route}: missing 'success'"
        assert "code" in data, f"{route}: missing 'code'"
        assert "message" in data, f"{route}: missing 'message'"
        assert "traceId" in data, f"{route}: missing 'traceId'"
