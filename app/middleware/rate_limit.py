from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.cache import get_redis_client

DEFAULT_LIMIT = 120
DEFAULT_WINDOW_SECONDS = 60

# Tighter limits for expensive or abuse-prone routes (requests per window).
ROUTE_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/search": (30, 60),
    "/auth/login": (10, 60),
    "/auth/register": (10, 60),
    "/feeds/public": (60, 60),
    "/feeds/home": (60, 60),
    "/feeds/personal": (60, 60),
    "/feeds/scope": (60, 60),
    "/bootstrap": (30, 60),
    "/bootstrap/summary": (120, 60),
    "/feedback": (5, 60),
}


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

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)

        path_limit, window_seconds = self._limits_for_path(request.url.path)
        client = request.client
        client_host = client.host if client and client.host else "unknown"
        window_bucket = int(time.time() // window_seconds)
        key = f"{self.key_prefix}:{client_host}:{request.method}:{request.url.path}:{window_bucket}"

        redis_client = get_redis_client()
        try:
            current_count = await redis_client.incr(key)
            if current_count == 1:
                await redis_client.expire(key, window_seconds + 1)
        except Exception:
            # Redis unavailable — allow traffic so outages do not take the API offline.
            return await call_next(request)

        if current_count > path_limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        return await call_next(request)
