"""
路由级速率限制
- 登录接口: 30次/15分钟 (防暴力破解)
- 刷新接口: 20次/分钟
- 昂贵接口(AI 诊断 / 批量命令 / 电源操作 / 控制台票据 / 容器日志): 单独收紧
- 默认全局: 100次/分钟
"""

import jwt
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import async_check_limits
from slowapi.util import get_remote_address
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Match
from starlette.types import Scope

from app.config import JWT_ALGORITHM, JWT_SECRET, RATE_LIMIT_STORAGE


def _key_func(request: Request) -> str:
    """速率限制 key：优先用认证用户ID，否则用IP"""
    user = getattr(request.state, "user", None)
    if user:
        uid = getattr(user, "id", None)
        if uid is not None:
            return f"user:{uid}"
    token = request.cookies.get("dcn_access")
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            if payload.get("type") == "access" and payload.get("sub"):
                return f"user:{payload['sub']}"
        except jwt.PyJWTError:
            pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=["100/minute"],
    storage_uri=RATE_LIMIT_STORAGE,
    # 按 endpoint 函数名分桶，而不是 slowapi 默认的按 URL 分桶。
    # /api/devices/{id} 这类带路径参数的路由，按 URL 分桶等于给每个 id 单开一个
    # 100/minute 的桶，换着 id 刷就能无限绕过。
    key_style="endpoint",
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


def _flatten(routes: list[BaseRoute]) -> list:
    """把路由树摊平成「可直接 matches() 且带 endpoint」的候选列表。

    FastAPI 0.141 起 include_router() 不再把子路由摊平进 app.routes，而是塞一个
    _IncludedRouter 包装对象。它没有 endpoint 属性，slowapi 自带的
    _find_route_handler 因此恒返回 None、_should_exempt 恒为 True —— 中间件对
    所有 include_router 注册的路由直接放行，全局默认限额形同虚设(本项目除
    /api/health 外全部路由都中招)。
    _IncludedRouter.effective_route_contexts() 给出的上下文已经带上了 include
    前缀编译好的 path_regex，直接拿它匹配即可，不必再穿过包装对象。
    """
    flat: list = []
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            flat.extend(contexts())
            # FastAPI 把 SPA/静态兜底一类的路由放在低优先级表里，同样要能匹配到，
            # 否则这些路径会绕过限流。
            low_priority = getattr(route, "effective_low_priority_routes", None)
            if callable(low_priority):
                flat.extend(low_priority())
        else:
            flat.append(route)
    return flat


def _resolve_endpoint(candidates: list, scope: Scope):
    """返回本次请求命中的路由函数；未命中返回 None。

    与 FastAPI 自身的路由语义一致：按注册顺序取第一个 FULL 匹配(slowapi 原版
    会一直遍历并取最后一个，既慢又和真实路由结果不一致)。
    """
    for candidate in candidates:
        try:
            match, _ = candidate.matches(scope)
        except Exception:  # noqa: BLE001 — 单个路由匹配异常不能拖垮整个请求
            continue
        if match != Match.FULL:
            continue
        endpoint = getattr(candidate, "endpoint", None)
        if endpoint is not None:
            return endpoint
    return None


def _is_exempt(app_limiter: Limiter, handler) -> bool:
    """被 @limiter.limit 装饰或显式豁免的路由，中间件让位给装饰器。

    与 slowapi._should_exempt 同义；内联在此避免依赖它的私有函数。
    """
    if handler is None:
        return True
    name = f"{handler.__module__}.{handler.__name__}"
    return name in app_limiter._exempt_routes or name in app_limiter._route_limits


class RateLimitMiddleware(BaseHTTPMiddleware):
    """替代 slowapi 的 SlowAPIMiddleware，修掉三个让限流失效/走样的问题。

    1. 路由解析兼容 FastAPI 0.141 的 _IncludedRouter(见 _flatten)——否则全局
       默认限额对所有 include_router 注册的路由完全不生效。
    2. 走 async_check_limits 而不是 sync_check_limits：后者遇到 async 异常处理器
       会退回 slowapi 自带的 {"error": ...} 响应体，本项目统一的
       success/code/message/traceId 格式就丢了。
    3. 命中第一个 FULL 匹配就返回，与 FastAPI 真实路由结果保持一致。
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._candidates: list | None = None
        self._candidates_route_count = -1

    def _candidates_for(self, app: Starlette) -> list:
        """路由表在启动后是静态的，摊平一次缓存复用，避免每个请求重复展开。

        仍以 len(app.routes) 作为廉价的失效判据，防止运行期动态挂载路由后
        拿着旧表匹配。
        """
        route_count = len(app.routes)
        if self._candidates is None or self._candidates_route_count != route_count:
            self._candidates = _flatten(app.routes)
            self._candidates_route_count = route_count
        return self._candidates

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        app: Starlette = request.app
        app_limiter: Limiter | None = getattr(app.state, "limiter", None)
        if app_limiter is None or not app_limiter.enabled:
            return await call_next(request)

        handler = _resolve_endpoint(self._candidates_for(app), request.scope)
        if _is_exempt(app_limiter, handler):
            return await call_next(request)

        error_response, should_inject_headers = await async_check_limits(
            app_limiter, request, handler, app
        )
        if error_response is not None:
            return error_response

        response = await call_next(request)
        if should_inject_headers:
            response = app_limiter._inject_headers(
                response, request.state.view_rate_limit
            )
        return response


def setup_rate_limiter(app):
    """将速率限制中间件注册到 FastAPI app"""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(RateLimitMiddleware)
