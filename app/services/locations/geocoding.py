from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from app.cache import get_redis_client
from app.config import Settings, get_settings

GEOCODING_RATE_LIMIT_PREFIX = "geocoding-rate-limit"
GEOCODING_CACHE_PREFIX = "geocoding-cache"
MAX_SEARCH_QUERY_LENGTH = 200
MAX_SEARCH_RESULTS = 10


def validate_search_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="geocoding_query_required",
        )
    if len(cleaned) > MAX_SEARCH_QUERY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="geocoding_query_too_long",
        )
    return cleaned


def validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_location_coordinates",
        ) from exc
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_coordinates_out_of_range",
        )
    return lat, lon


async def enforce_geocoding_rate_limit(request: Request) -> None:
    from app.utils.request import get_client_ip

    settings = get_settings()
    client_host = get_client_ip(request)
    window = max(1, settings.geocoding_rate_limit_window_seconds)
    window_bucket = int(time.time() // window)
    key = f"{GEOCODING_RATE_LIMIT_PREFIX}:{client_host}:{window_bucket}"

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

    if current_count > settings.geocoding_rate_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="geocoding_rate_limit_exceeded",
        )


def _cache_key(kind: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{GEOCODING_CACHE_PREFIX}:{kind}:{digest}"


async def _cache_get(key: str) -> list[dict[str, Any]] | None:
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
    if not isinstance(parsed, list):
        return None
    return parsed


async def _cache_set(key: str, value: list[dict[str, Any]], ttl: int) -> None:
    redis_client: Redis = get_redis_client()
    try:
        await redis_client.set(key, json.dumps(value), ex=max(60, ttl))
    except Exception:
        return


def _normalize_nominatim_result(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    region = (
        address.get("state")
        or address.get("region")
        or address.get("county")
        or address.get("city")
    )
    country = address.get("country")
    display_label = str(item.get("display_name") or "").strip()
    if not display_label:
        return None

    place_id = item.get("place_id")
    osm_type = item.get("osm_type")
    osm_id = item.get("osm_id")
    if place_id is not None:
        provider_place_id = f"nominatim:{place_id}"
    elif osm_type and osm_id is not None:
        provider_place_id = f"osm:{osm_type}:{osm_id}"
    else:
        provider_place_id = None

    return {
        "provider_place_id": provider_place_id,
        "display_label": display_label[:240],
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "region": str(region)[:120] if region else None,
        "country": str(country)[:120] if country else None,
        "precision": "approximate",
        "is_online": False,
    }


async def _provider_get(
    path: str,
    params: dict[str, Any],
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    base = settings.geocoding_provider_url.rstrip("/")
    query = dict(params)
    if settings.geocoding_provider_api_key.strip():
        query["key"] = settings.geocoding_provider_api_key.strip()

    url = f"{base}{path}?{urlencode(query)}"
    headers = {
        "User-Agent": settings.geocoding_user_agent,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.geocoding_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="geocoding_provider_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="geocoding_provider_unavailable",
        ) from exc

    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="geocoding_provider_rate_limited",
        )
    if response.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="geocoding_provider_unavailable",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="geocoding_provider_error",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="geocoding_provider_invalid_response",
        ) from exc

    if isinstance(payload, dict):
        items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_nominatim_result(item)
        if normalized is not None:
            results.append(normalized)
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    return results


async def search_places(
    query: str,
    *,
    limit: int = 5,
    country_codes: str | None = None,
    viewbox: tuple[float, float, float, float] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    runtime = settings or get_settings()
    cleaned = validate_search_query(query)
    safe_limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))

    cache_payload: dict[str, Any] = {
        "q": cleaned.lower(),
        "limit": safe_limit,
        "country_codes": (country_codes or "").strip().lower(),
    }
    if viewbox is not None:
        cache_payload["viewbox"] = [round(v, 4) for v in viewbox]
    key = _cache_key("search", cache_payload)
    cached = await _cache_get(key)
    if cached is not None:
        return cached[:safe_limit]

    params: dict[str, Any] = {
        "q": cleaned,
        "format": "json",
        "addressdetails": 1,
        "limit": safe_limit,
    }
    if country_codes and country_codes.strip():
        params["countrycodes"] = country_codes.strip().lower()
    if viewbox is not None:
        min_lon, max_lat, max_lon, min_lat = viewbox
        params["viewbox"] = f"{min_lon},{max_lat},{max_lon},{min_lat}"
        params["bounded"] = 0

    results = await _provider_get(
        "/search",
        params,
        settings=runtime,
    )
    await _cache_set(key, results, runtime.geocoding_cache_ttl_seconds)
    return results


async def reverse_geocode(
    latitude: float,
    longitude: float,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    runtime = settings or get_settings()
    lat, lon = validate_coordinates(latitude, longitude)

    cache_payload = {"lat": round(lat, 6), "lon": round(lon, 6)}
    key = _cache_key("reverse", cache_payload)
    cached = await _cache_get(key)
    if cached is not None:
        return cached

    results = await _provider_get(
        "/reverse",
        {
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "format": "json",
            "addressdetails": 1,
        },
        settings=runtime,
    )
    await _cache_set(key, results, runtime.geocoding_cache_ttl_seconds)
    return results
