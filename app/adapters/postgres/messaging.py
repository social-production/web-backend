"""Postgres-backed MessagingProvider."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import messages as messages_service


class PostgresMessagingProvider:
    def __init__(self, db: Session) -> None:
        self._db = db

    def list_conversations(self, current_user_id: UUID) -> dict[str, object]:
        return messages_service.list_conversations(self._db, current_user_id)

    def get_messages(
        self,
        current_user_id: UUID,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        return messages_service.get_messages_for_conversation(
            self._db,
            current_user_id,
            conversation_id,
            limit=limit,
            offset=offset,
        )

    def send_message(
        self,
        current_user_id: UUID,
        conversation_id: UUID,
        body: str,
    ) -> dict[str, object]:
        return messages_service.send_message(
            self._db,
            current_user_id,
            conversation_id,
            body,
        )

    def search_contacts(
        self,
        current_user_id: UUID,
        *,
        query: str = "",
        limit: int = 8,
    ) -> dict[str, object]:
        return messages_service.search_message_contacts(
            self._db,
            current_user_id,
            query=query,
            limit=limit,
        )

    def get_linked_chats(self, current_user_id: UUID) -> dict[str, object]:
        return messages_service.get_linked_chats(self._db, current_user_id)
