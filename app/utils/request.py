from __future__ import annotations

from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Resolve the client IP, honoring X-Forwarded-For from reverse proxies (e.g. Railway).

    The website's /api proxy sends the browser's address as X-Client-IP, because Railway's
    edge in front of the API replaces X-Forwarded-For with the website server's address.
    """
    proxied_client = request.headers.get("x-client-ip")
    if proxied_client:
        return proxied_client.split(",")[0].strip()

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    client = request.client
    if client and client.host:
        return client.host

    return "unknown"
