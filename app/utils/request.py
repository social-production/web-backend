from __future__ import annotations

from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Resolve the client IP, honoring X-Forwarded-For from reverse proxies (e.g. Railway)."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    client = request.client
    if client and client.host:
        return client.host

    return "unknown"
