"""统一异常层级 — 所有业务错误应使用这些异常类而非裸 HTTPException"""

from typing import Any


class AppError(Exception):
    """所有自定义业务异常的基类"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, resource: str, details: dict | None = None):
        super().__init__("NOT_FOUND", f"{resource}不存在", 404, details)


class AuthError(AppError):
    def __init__(self, message: str = "认证失败", details: dict | None = None):
        super().__init__("AUTH_ERROR", message, 401, details)


class TokenExpiredError(AppError):
    def __init__(self, message: str = "登录已过期，请重新登录"):
        super().__init__("AUTH_TOKEN_EXPIRED", message, 401)


class TokenInvalidError(AppError):
    def __init__(self, message: str = "认证凭据无效"):
        super().__init__("AUTH_TOKEN_INVALID", message, 401)


class ForbiddenError(AppError):
    def __init__(self, message: str = "权限不足", details: dict | None = None):
        super().__init__("FORBIDDEN", message, 403, details)


class ValidationError(AppError):
    def __init__(self, message: str = "请求参数验证失败", details: dict | None = None):
        super().__init__("VALIDATION_ERROR", message, 422, details)


class ConflictError(AppError):
    def __init__(self, message: str = "资源已存在", details: dict | None = None):
        super().__init__("CONFLICT", message, 409, details)


class RateLimitError(AppError):
    def __init__(self, message: str = "请求过于频繁，请稍后再试"):
        super().__init__("RATE_LIMIT_EXCEEDED", message, 429)


class BusinessError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__("BUSINESS_ERROR", message, 400, details)
