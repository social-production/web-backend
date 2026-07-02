from __future__ import annotations

from functools import lru_cache

import redis as sync_redis
from redis.asyncio import Redis

from app.config import get_settings


def _redis_client_kwargs() -> dict[str, object]:
    settings = get_settings()
    return {
        "decode_responses": True,
        "socket_timeout": settings.redis_socket_timeout_seconds,
        "socket_connect_timeout": settings.redis_socket_connect_timeout_seconds,
        "max_connections": settings.redis_max_connections,
        "health_check_interval": 30,
    }


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, **_redis_client_kwargs())


@lru_cache(maxsize=1)
def get_sync_redis_client() -> sync_redis.Redis:
    settings = get_settings()
    return sync_redis.Redis.from_url(settings.redis_url, **_redis_client_kwargs())


def cache_ttl_seconds() -> int:
    return max(60, get_settings().redis_cache_ttl_seconds)


async def close_redis_client() -> None:
    client = get_redis_client()
    await client.aclose()
    get_redis_client.cache_clear()
    get_sync_redis_client.cache_clear()
