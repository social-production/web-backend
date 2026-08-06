"""Postgres-backed AccessPolicy wrapping existing access_control service."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import access_control as access_control_service


class PostgresAccessPolicy:
    def __init__(self, db: Session) -> None:
        self._db = db

    def can_view_entity(
        self,
        viewer_id: UUID | None,
        entity_type: str,
        entity_id: UUID,
    ) -> bool:
        return access_control_service.can_view_entity(self._db, viewer_id, entity_type, entity_id)

    def assert_can_view_entity(
        self,
        viewer_id: UUID | None,
        entity_type: str,
        entity_id: UUID,
    ) -> None:
        access_control_service.assert_can_view_entity(self._db, viewer_id, entity_type, entity_id)

    def assert_can_view_subject(
        self,
        viewer_id: UUID | None,
        subject_type: str,
        subject_id: UUID,
    ) -> None:
        access_control_service.assert_can_view_subject(
            self._db, viewer_id, subject_type, subject_id
        )
