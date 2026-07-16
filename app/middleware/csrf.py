from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.auth.cookies import CSRF_COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_EXEMPT_PREFIXES = (
    "/healthz",
    "/readyz",
    "/auth/login",
    "/auth/register",
    "/auth/refresh",
    "/docs",
    "/openapi.json",
)


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE)
        header_token = request.headers.get("x-csrf-token")
        if not cookie_token or not header_token or cookie_token != header_token:
            return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})

        return await call_next(request)
