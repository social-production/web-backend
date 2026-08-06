"""Redis-backed JWT blacklist / token revocation."""

from __future__ import annotations

from app.adapters.redis.cache_store import RedisCacheStore
from app.errors import ServiceUnavailableAppError
from app.ports import CacheStore, TokenRevocationStore

TOKEN_BLACKLIST_PREFIX = "token-blacklist"


class RedisTokenRevocationStore:
    def __init__(
        self,
        cache: CacheStore | None = None,
        *,
        fail_closed: bool = False,
    ) -> None:
        self._cache = cache or RedisCacheStore()
        self._fail_closed = fail_closed

    def _key(self, jti: str) -> str:
        return f"{TOKEN_BLACKLIST_PREFIX}:{jti}"

    async def is_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        try:
            return await self._cache.exists(self._key(jti))
        except Exception:
            if self._fail_closed:
                raise ServiceUnavailableAppError("Authentication service temporarily unavailable")
            return False

    async def revoke(self, jti: str, *, ttl_seconds: int) -> None:
        await self._cache.setex(self._key(jti), max(1, ttl_seconds), "1")


def get_token_revocation_store(*, fail_closed: bool | None = None) -> TokenRevocationStore:
    from app.config import get_settings

    settings = get_settings()
    closed = settings.rate_limit_fail_closed if fail_closed is None else fail_closed
    return RedisTokenRevocationStore(fail_closed=closed)
