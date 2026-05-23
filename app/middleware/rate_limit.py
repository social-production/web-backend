from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.cache import get_redis_client


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, limit: int = 120, window_seconds: int = 60, key_prefix: str = "rate-limit"):
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)

        client = request.client
        client_host = client.host if client and client.host else "unknown"
        window_bucket = int(time.time() // self.window_seconds)
        key = f"{self.key_prefix}:{client_host}:{request.method}:{request.url.path}:{window_bucket}"

        redis_client = get_redis_client()
        try:
            current_count = await redis_client.incr(key)
            if current_count == 1:
                await redis_client.expire(key, self.window_seconds + 1)
        except Exception:
            return await call_next(request)

        if current_count > self.limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

        return await call_next(request)
