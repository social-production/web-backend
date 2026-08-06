"""JWT AuthProvider wrapping existing auth service functions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.redis.token_revocation import get_token_revocation_store
from app.services import auth as auth_service


class JwtAuthProvider:
    """Current production auth — swap for SupabaseAuthProvider later."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def register(
        self,
        *,
        username: str,
        password: str,
        profile_bio: str | None = None,
    ) -> dict[str, object]:
        return auth_service.register_user(
            self._db,
            username=username,
            password=password,
            profile_bio=profile_bio,
        )

    def authenticate(self, *, username: str, password: str) -> dict[str, object]:
        return auth_service.authenticate_user(
            self._db,
            username=username,
            password=password,
        )

    async def refresh(self, *, refresh_token: str) -> dict[str, object]:
        return await auth_service.refresh_auth_session(refresh_token=refresh_token)

    async def revoke_access(self, *, jti: str, ttl_seconds: int) -> None:
        store = get_token_revocation_store()
        await store.revoke(jti, ttl_seconds=ttl_seconds)
