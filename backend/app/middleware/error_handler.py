"""中心化异常处理 — 将所有错误转为统一 JSON 格式"""

import logging
import traceback

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.exceptions import AppError

logger = logging.getLogger(__name__)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """处理自定义 AppError"""
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.warning(
        "[%s] %s %s | %s: %s",
        trace_id[:12],
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    body: dict = {
        "success": False,
        "code": exc.code,
        "message": exc.message,
        "traceId": trace_id,
    }
    if exc.details:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """处理 Pydantic 校验错误"""
    trace_id = getattr(request.state, "trace_id", "unknown")
    errors = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", []))
        errors.append(f"{loc}: {err.get('msg', '')}")
    message = "; ".join(errors)
    logger.warning("[%s] Validation error: %s", trace_id[:12], message)
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "code": "VALIDATION_ERROR",
            "message": f"请求参数验证失败: {message}",
            "traceId": trace_id,
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 — 处理所有未预期的异常"""
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.error(
        "[%s] Unhandled exception on %s %s: %s\n%s",
        trace_id[:12],
        request.method,
        request.url.path,
        exc,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "code": "SERVER_ERROR",
            "message": "服务器内部错误，请稍后重试",
            "traceId": trace_id,
        },
    )
