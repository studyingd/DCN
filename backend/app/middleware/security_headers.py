"""
Security headers middleware for FastAPI.
Adds HTTP security headers to all API responses.

Mirrors the Nginx security headers so the backend is secure even
when accessed directly (e.g., during development or internal networking).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import COOKIE_SECURE


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security-related HTTP headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        headers = response.headers

        # Prevent MIME-type sniffing
        headers.setdefault("X-Content-Type-Options", "nosniff")

        # Clickjacking protection
        headers.setdefault("X-Frame-Options", "SAMEORIGIN")

        # Legacy XSS filter
        headers.setdefault("X-XSS-Protection", "1; mode=block")

        # HTTP Strict Transport Security — only emit when serving over TLS, so it
        # doesn't pin a dev HTTP deployment. nginx should also set it.
        if COOKIE_SECURE or request.url.scheme == "https":
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        # Referrer policy
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

        # Content Security Policy (API responses — restrict to JSON/default)
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        )

        # Permissions Policy
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )

        # Prevent IE from sniffing content types
        headers.setdefault("X-Download-Options", "noopen")

        # DNS prefetch control
        headers.setdefault("X-DNS-Prefetch-Control", "off")

        return response
