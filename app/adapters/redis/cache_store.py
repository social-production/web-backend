"""Redis-backed CacheStore."""

from __future__ import annotations

from redis.asyncio import Redis

from app.cache import get_redis_client


class RedisCacheStore:
    def __init__(self, client: Redis | None = None) -> None:
        self._client = client or get_redis_client()

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is None:
            await self._client.set(key, value)
        else:
            await self._client.setex(key, ttl_seconds, value)

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        await self._client.setex(key, ttl_seconds, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(key))

    async def incr(self, key: str) -> int:
        return int(await self._client.incr(key))

    async def ping(self) -> bool:
        return bool(await self._client.ping())
