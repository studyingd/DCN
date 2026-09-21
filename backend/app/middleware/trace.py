"""请求追踪中间件 — 为每个请求分配唯一 traceId

使用纯 ASGI 中间件而非 BaseHTTPMiddleware，确保异常能正确
流入 FastAPI 的异常处理器（尤其是通用的 Exception handler）。
"""

import logging
import time
import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

from app.logging_config import reset_trace_id, set_trace_id
from app.utils import sanitize_for_log

logger = logging.getLogger(__name__)


class TraceMiddleware:
    """纯 ASGI 中间件，为每个请求分配 traceId 并记录请求日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 从请求头中提取或生成 traceId
        headers = dict(scope.get("headers", []))
        trace_id_bytes = headers.get(b"x-trace-id")
        if trace_id_bytes:
            # Client-supplied: sanitize control chars (log-injection defense) and
            # cap length, mirroring the path sanitization further below.
            trace_id = sanitize_for_log(
                trace_id_bytes.decode("utf-8", errors="replace")[:64]
            )
        else:
            trace_id = str(uuid.uuid4())

        # 将 traceId 存入 scope 的 state 中
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["trace_id"] = trace_id

        start = time.monotonic()

        # 拦截 send 以便在响应头中注入 X-Trace-Id
        status_code: int = 200
        trace_id_injected = False

        async def send_with_trace(message: dict) -> None:
            nonlocal status_code, trace_id_injected
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
                if not trace_id_injected:
                    headers = list(message.get("headers", []))
                    headers.append((b"x-trace-id", trace_id.encode("utf-8")))
                    message["headers"] = headers
                    trace_id_injected = True
            await send(message)

        # 绑定到 contextvar，服务层/路由层的 logger 调用也会带上同一个 traceId。
        # 同步路由跑在线程池里，anyio 会复制上下文，因此同样能取到。
        trace_token = set_trace_id(trace_id)
        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            path = scope.get("path", "?")
            # Sanitize path to prevent log injection via control characters
            safe_path = path.translate(
                {
                    ord(c): f"\\x{ord(c):02x}"
                    for c in "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
                }
            )
            safe_path = (
                safe_path.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            )
            method = scope.get("method", "?")

            if status_code >= 500:
                log_fn = logger.error
            elif status_code >= 400:
                log_fn = logger.warning
            else:
                log_fn = logger.info

            log_fn(
                "[%s] %s %s → %s (%.1fms)",
                trace_id[:12],
                method,
                safe_path,
                status_code,
                duration_ms,
            )
            reset_trace_id(trace_token)
