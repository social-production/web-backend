from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto.messages import decrypt_message, encrypt_message
from app.models import (
    conversation_members,
    conversations,
    messages,
)
from app.services.messages.conversations import (
    _conversation_unread_count,
    _ensure_member,
    _get_conversation_row,
)
from app.services.messages.linked_chats import get_linked_chats


def _serialize_message(row: Mapping[str, object], body: str) -> dict[str, object]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "body": body,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_total_unread_message_count(db: Session, current_user_id: UUID) -> int:
    conversation_rows = db.execute(
        select(conversations.c.id, conversation_members.c.last_read_at)
        .select_from(
            conversations.join(
                conversation_members,
                conversations.c.id == conversation_members.c.conversation_id,
            )
        )
        .where(conversation_members.c.user_id == current_user_id)
    ).all()

    total = 0
    for conversation_id, last_read_at in conversation_rows:
        total += _conversation_unread_count(db, conversation_id, current_user_id, last_read_at)

    linked_chats = get_linked_chats(db, current_user_id)
    total += sum(int(item.get("unread_count") or 0) for item in linked_chats["items"])
    return total


def send_message(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    body: str,
) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)

    plaintext = body.strip()
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message body is required"
        )

    encrypted = encrypt_message(plaintext)
    now = datetime.now(UTC)

    try:
        created = (
            db.execute(
                insert(messages)
                .values(
                    conversation_id=conversation_id,
                    sender_id=current_user_id,
                    encrypted_body=encrypted,
                    encryption_version=1,
                )
                .returning(
                    messages.c.id,
                    messages.c.conversation_id,
                    messages.c.sender_id,
                    messages.c.encrypted_body,
                    messages.c.created_at,
                    messages.c.updated_at,
                )
            )
            .mappings()
            .one()
        )

        db.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(last_message_at=now)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not send message"
        ) from exc

    return {"message": _serialize_message(created, plaintext)}


def get_messages_for_conversation(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)

    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)

    total = db.execute(
        select(func.count())
        .select_from(messages)
        .where(messages.c.conversation_id == conversation_id)
    ).scalar_one()

    rows = (
        db.execute(
            select(messages)
            .where(messages.c.conversation_id == conversation_id)
            .order_by(messages.c.created_at.asc())
            .limit(safe_limit)
            .offset(safe_offset)
        )
        .mappings()
        .all()
    )

    items = []
    for row in rows:
        try:
            plaintext = decrypt_message(row["encrypted_body"])
        except InvalidToken as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored message could not be decrypted",
            ) from exc
        items.append(_serialize_message(row, plaintext))

    return {
        "conversation_id": conversation_id,
        "total": int(total or 0),
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
    }


def mark_conversation_as_read(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)

    now = datetime.now(UTC)
    db.execute(
        update(conversation_members)
        .where(
            conversation_members.c.conversation_id == conversation_id,
            conversation_members.c.user_id == current_user_id,
        )
        .values(last_read_at=now)
    )
    db.commit()
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "last_read_at": now,
    }
