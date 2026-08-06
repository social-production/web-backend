"""Postgres-backed NotificationsProvider."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import notifications as notifications_service


class PostgresNotificationsProvider:
    def __init__(self, db: Session) -> None:
        self._db = db

    def list_notifications(
        self,
        current_user_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        return notifications_service.list_notifications(
            self._db,
            current_user_id,
            unread_only=unread_only,
            limit=limit,
            offset=offset,
        )

    def mark_read(self, current_user_id: UUID, notification_id: UUID) -> dict[str, object]:
        return notifications_service.mark_notification_read(
            self._db, current_user_id, notification_id
        )

    def mark_all_read(self, current_user_id: UUID) -> dict[str, object]:
        return notifications_service.mark_all_notifications_read(self._db, current_user_id)
