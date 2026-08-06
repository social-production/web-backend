"""Provider ports — stable interfaces for infra-backed capabilities.

Current implementations live under ``app.adapters``. Future providers
(Supabase Auth, alternate cache, etc.) implement these Protocols.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class CacheStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def incr(self, key: str) -> int: ...

    async def ping(self) -> bool: ...


class TokenRevocationStore(Protocol):
    """Tracks revoked JWT jtis (blacklist)."""

    async def is_revoked(self, jti: str) -> bool: ...

    async def revoke(self, jti: str, *, ttl_seconds: int) -> None: ...


class SearchProvider(Protocol):
    def index_document(
        self,
        *,
        entity_type: str,
        entity_id: UUID,
        title: str,
        summary: str,
        meta: str,
        href: str,
    ) -> dict[str, object]: ...

    def search(
        self,
        *,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 20,
        viewer_id: UUID | None = None,
    ) -> dict[str, object]: ...


class AccessPolicy(Protocol):
    """Visibility checks without exposing SQL fragments to callers."""

    def can_view_entity(
        self,
        viewer_id: UUID | None,
        entity_type: str,
        entity_id: UUID,
    ) -> bool: ...

    def assert_can_view_entity(
        self,
        viewer_id: UUID | None,
        entity_type: str,
        entity_id: UUID,
    ) -> None: ...

    def assert_can_view_subject(
        self,
        viewer_id: UUID | None,
        subject_type: str,
        subject_id: UUID,
    ) -> None: ...


class AuthProvider(Protocol):
    """Authentication lifecycle — current JWT implementation; Supabase later."""

    def register(
        self,
        *,
        username: str,
        password: str,
        profile_bio: str | None = None,
    ) -> dict[str, object]: ...

    def authenticate(self, *, username: str, password: str) -> dict[str, object]: ...

    async def refresh(self, *, refresh_token: str) -> dict[str, object]: ...

    async def revoke_access(self, *, jti: str, ttl_seconds: int) -> None: ...


class PeopleSuggestionsProvider(Protocol):
    def suggest(
        self,
        current_user_id: UUID,
        *,
        query: str | None = None,
        limit: int = 12,
        exclude_ids: set[UUID] | None = None,
    ) -> list[dict[str, object]]: ...


class NotificationsProvider(Protocol):
    def list_notifications(
        self,
        current_user_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]: ...

    def mark_read(self, current_user_id: UUID, notification_id: UUID) -> dict[str, object]: ...

    def mark_all_read(self, current_user_id: UUID) -> dict[str, object]: ...


class MessagingProvider(Protocol):
    def list_conversations(self, current_user_id: UUID) -> dict[str, object]: ...

    def get_messages(
        self,
        current_user_id: UUID,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]: ...

    def send_message(
        self,
        current_user_id: UUID,
        conversation_id: UUID,
        body: str,
    ) -> dict[str, object]: ...

    def search_contacts(
        self,
        current_user_id: UUID,
        *,
        query: str = "",
        limit: int = 8,
    ) -> dict[str, object]: ...

    def get_linked_chats(self, current_user_id: UUID) -> dict[str, object]: ...


class FeedProvider(Protocol):
    def get_public_feed(
        self,
        *,
        sort: str = "trending",
        limit: int = 20,
        offset: int = 0,
        current_user_id: UUID | None = None,
        window: str = "all",
        entity_filter: str = "all",
    ) -> dict[str, object]: ...

    def get_home_feed(
        self,
        current_user_id: UUID,
        *,
        sort: str = "trending",
        limit: int = 20,
        offset: int = 0,
        window: str = "all",
        entity_filter: str = "all",
    ) -> dict[str, object]: ...

    def get_personal_feed(
        self,
        current_user_id: UUID,
        *,
        sort: str = "trending",
        limit: int = 20,
        offset: int = 0,
        scope: str = "following",
        window: str = "all",
        entity_filter: str = "all",
    ) -> dict[str, object]: ...
