from __future__ import annotations

import json
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from app.cache import get_redis_client
from app.config import get_settings
from app.services.locations.geocoding import (
    validate_coordinates,
)

IP_HINT_CACHE_PREFIX = "ip-location-hint"
IP_HINT_RATE_LIMIT_PREFIX = "ip-location-rate-limit"


async def enforce_ip_hint_rate_limit(request: Request) -> None:
    from app.utils.request import get_client_ip

    settings = get_settings()
    client_host = get_client_ip(request)
    window = max(1, settings.geocoding_rate_limit_window_seconds)
    window_bucket = int(time.time() // window)
    key = f"{IP_HINT_RATE_LIMIT_PREFIX}:{client_host}:{window_bucket}"

    redis_client: Redis = get_redis_client()
    try:
        current_count = await redis_client.incr(key)
        if current_count == 1:
            await redis_client.expire(key, window + 1)
    except Exception:
        if settings.rate_limit_fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting service temporarily unavailable",
            )
        return

    if current_count > 10:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="ip_location_rate_limit_exceeded",
        )


async def _cache_get_ip_hint(key: str) -> dict[str, Any] | None:
    redis_client: Redis = get_redis_client()
    try:
        raw = await redis_client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _cache_set_ip_hint(key: str, value: dict[str, Any], ttl: int) -> None:
    redis_client: Redis = get_redis_client()
    try:
        await redis_client.set(key, json.dumps(value), ex=max(300, ttl))
    except Exception:
        return


async def ip_location_hint(request: Request) -> dict[str, Any]:
    """Approximate location from client IP. Only called on explicit user opt-in."""
    from app.utils.request import get_client_ip

    await enforce_ip_hint_rate_limit(request)
    settings = get_settings()
    client_host = get_client_ip(request)
    if not client_host or client_host in {"127.0.0.1", "::1"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ip_location_unavailable",
        )

    cache_key = f"{IP_HINT_CACHE_PREFIX}:{client_host}"
    cached = await _cache_get_ip_hint(cache_key)
    if cached is not None:
        return cached

    url = f"http://ip-api.com/json/{client_host}?fields=status,message,country,regionName,city,lat,lon"
    try:
        async with httpx.AsyncClient(timeout=settings.geocoding_timeout_seconds) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ip_location_provider_unavailable",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ip_location_provider_unavailable",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ip_location_provider_invalid_response",
        ) from exc

    if payload.get("status") != "success":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ip_location_unavailable",
        )

    lat = float(payload["lat"])
    lon = float(payload["lon"])
    validate_coordinates(lat, lon)
    city = str(payload.get("city") or "").strip()
    region = str(payload.get("regionName") or "").strip()
    country = str(payload.get("country") or "").strip()
    label_parts = [part for part in [city, region, country] if part]
    display_label = ", ".join(label_parts) if label_parts else f"{lat:.2f}, {lon:.2f}"

    result = {
        "provider_place_id": None,
        "display_label": display_label[:240],
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "region": region[:120] if region else None,
        "country": country[:120] if country else None,
        "precision": "approximate",
        "is_online": False,
    }
    await _cache_set_ip_hint(cache_key, result, settings.geocoding_cache_ttl_seconds)
    return result
