"""Postgres-backed people suggestions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.people_suggestions import get_ranked_people_suggestions


class PostgresPeopleSuggestionsProvider:
    def __init__(self, db: Session) -> None:
        self._db = db

    def suggest(
        self,
        current_user_id: UUID,
        *,
        query: str | None = None,
        limit: int = 12,
        exclude_ids: set[UUID] | None = None,
    ) -> list[dict[str, object]]:
        return get_ranked_people_suggestions(
            self._db,
            current_user_id,
            query=query,
            limit=limit,
            exclude_ids=exclude_ids,
        )
