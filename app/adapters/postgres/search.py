"""Postgres-backed SearchProvider wrapping existing search service."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import search as search_service


class PostgresSearchProvider:
    def __init__(self, db: Session) -> None:
        self._db = db

    def index_document(
        self,
        *,
        entity_type: str,
        entity_id: UUID,
        title: str,
        summary: str,
        meta: str,
        href: str,
    ) -> dict[str, object]:
        return search_service.index_document(
            self._db,
            entity_type,
            entity_id,
            title,
            summary,
            meta,
            href,
        )

    def search(
        self,
        *,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 20,
        viewer_id: UUID | None = None,
    ) -> dict[str, object]:
        return search_service.search_documents(
            self._db,
            query,
            entity_types,
            limit,
            viewer_id,
        )
