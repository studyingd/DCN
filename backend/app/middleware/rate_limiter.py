"""
路由级速率限制
- 登录接口: 5次/15分钟 (防暴力破解)
- 认证接口: 20次/分钟
- 默认全局: 100次/分钟
"""

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import RATE_LIMIT_STORAGE


def _key_func(request: Request) -> str:
    """速率限制 key：优先用认证用户ID，否则用IP"""
    user = getattr(request.state, "user", None)
    if user:
        uid = getattr(user, "id", None)
        if uid is not None:
            return f"user:{uid}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=["100/minute"],
    storage_uri=RATE_LIMIT_STORAGE,
)


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """自定义 429 响应格式 — 与统一错误处理格式对齐"""
    trace_id = getattr(request.state, "trace_id", "unknown")
    retry_after = str(exc.retry_after) if getattr(exc, "retry_after", None) else "60"
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "code": "RATE_LIMIT_EXCEEDED",
            "message": "请求过于频繁，请稍后再试",
            "traceId": trace_id,
        },
        headers={"Retry-After": retry_after},
    )


def setup_rate_limiter(app):
    """将速率限制中间件注册到 FastAPI app"""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
