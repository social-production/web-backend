"""Postgres-backed FeedProvider."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.feeds import get_home_feed, get_personal_feed, get_public_feed


class PostgresFeedProvider:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_public_feed(
        self,
        *,
        sort: str = "trending",
        limit: int = 20,
        offset: int = 0,
        current_user_id: UUID | None = None,
        window: str = "all",
        entity_filter: str = "all",
    ) -> dict[str, object]:
        return get_public_feed(
            self._db,
            sort=sort,
            limit=limit,
            offset=offset,
            current_user_id=current_user_id,
            window=window,
            entity_filter=entity_filter,
        )

    def get_home_feed(
        self,
        current_user_id: UUID,
        *,
        sort: str = "trending",
        limit: int = 20,
        offset: int = 0,
        window: str = "all",
        entity_filter: str = "all",
    ) -> dict[str, object]:
        return get_home_feed(
            self._db,
            current_user_id,
            sort=sort,
            limit=limit,
            offset=offset,
            window=window,
            entity_filter=entity_filter,
        )

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
    ) -> dict[str, object]:
        return get_personal_feed(
            self._db,
            current_user_id,
            sort=sort,
            limit=limit,
            offset=offset,
            scope=scope,
            window=window,
            entity_filter=entity_filter,
        )
