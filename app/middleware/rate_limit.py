from __future__ import annotations

import time
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.auth.cookies import ACCESS_COOKIE
from app.auth.jwt import JWTError, get_access_token_payload
from app.cache import get_redis_client
from app.config import get_settings
from app.utils.request import get_client_ip

DEFAULT_LIMIT = 120
DEFAULT_WINDOW_SECONDS = 60

ROUTE_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/search": (30, 60),
    "/auth/login": (10, 60),
    "/auth/register": (10, 60),
    "/auth/refresh": (20, 60),
    "/feeds/public": (60, 60),
    "/feeds/home": (60, 60),
    "/feeds/personal": (60, 60),
    "/feeds/scope": (60, 60),
    "/bootstrap": (30, 60),
    "/bootstrap/summary": (120, 60),
    "/feedback": (5, 60),
}

USER_LIMITED_PREFIXES = (
    "/search",
    "/bootstrap",
    "/feeds/home",
    "/feeds/personal",
    "/feeds/public",
    "/feeds/scope",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        limit: int = DEFAULT_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        key_prefix: str = "rate-limit",
    ):
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix

    def _limits_for_path(self, path: str) -> tuple[int, int]:
        if path in ROUTE_RATE_LIMITS:
            return ROUTE_RATE_LIMITS[path]
        for prefix, limits in ROUTE_RATE_LIMITS.items():
            if prefix.endswith("/") and path.startswith(prefix):
                return limits
        return self.limit, self.window_seconds

    def _user_rate_key(self, request: Request) -> str | None:
        if not any(request.url.path.startswith(prefix) for prefix in USER_LIMITED_PREFIXES):
            return None

        token = request.cookies.get(ACCESS_COOKIE)
        if not token:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                token = authorization[7:].strip()

        if not token:
            return None

        try:
            payload = get_access_token_payload(token)
        except JWTError:
            return None

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            return None

        try:
            user_id = UUID(subject)
        except ValueError:
            return None

        return str(user_id)

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)

        path_limit, window_seconds = self._limits_for_path(request.url.path)
        client_host = get_client_ip(request)
        window_bucket = int(time.time() // window_seconds)
        user_id = self._user_rate_key(request)
        identity = f"user:{user_id}" if user_id else f"ip:{client_host}"
        key = f"{self.key_prefix}:{identity}:{request.method}:{request.url.path}:{window_bucket}"

        redis_client = get_redis_client()
        try:
            current_count = await redis_client.incr(key)
            if current_count == 1:
                await redis_client.expire(key, window_seconds + 1)
        except Exception:
            if get_settings().rate_limit_fail_closed:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Rate limiting service temporarily unavailable"},
                )
            return await call_next(request)

        if current_count > path_limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        return await call_next(request)
