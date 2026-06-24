from __future__ import annotations

from functools import lru_cache

import redis as sync_redis
from redis.asyncio import Redis

from app.config import get_settings


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    settings = get_settings()
    return Redis.from_url(settings.redis_url, decode_responses=True)


@lru_cache(maxsize=1)
def get_sync_redis_client() -> sync_redis.Redis:
    settings = get_settings()
    return sync_redis.Redis.from_url(settings.redis_url, decode_responses=True)


async def close_redis_client() -> None:
    client = get_redis_client()
    await client.aclose()
    get_redis_client.cache_clear()
