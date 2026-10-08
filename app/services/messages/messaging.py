from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.postgres.blob_store import PostgresBlobStore
from app.crypto.messages import decrypt_message, encrypt_bytes, encrypt_message
from app.models import (
    conversation_members,
    conversation_pins,
    conversations,
    message_attachments,
    messages,
    users,
)
from app.services.messages.attachments import (
    PreparedAttachment,
    attachments_by_message,
    list_pins,
    viewer_can_pin,
)
from app.services.messages.conversations import (
    _conversation_unread_count,
    _ensure_member,
    _get_conversation_row,
)
from app.services.messages.linked_chats import get_linked_chats
from app.services.moderation.serialize import (
    load_active_reports_for_targets,
    moderation_body_for_comment,
)


def _serialize_message(
    row: Mapping[str, object],
    body: str,
    *,
    report: dict[str, object] | None = None,
    attachments: list[dict[str, object]] | None = None,
    reply: dict[str, object] | None = None,
) -> dict[str, object]:
    moderation_state = str(row.get("moderation_state") or "visible")
    moderation_reason = row.get("moderation_reason")
    display_body = moderation_body_for_comment(
        body=body,
        moderation_state=moderation_state,
        moderation_reason=str(moderation_reason) if moderation_reason else None,
    )
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "body": display_body,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "edited_at": row.get("edited_at"),
        "reply_to_id": row.get("reply_to_id"),
        "reply_author": None if reply is None else reply.get("author"),
        "reply_preview": None if reply is None else reply.get("preview"),
        "moderation_state": moderation_state,
        "moderation_reason": moderation_reason,
        "report": report,
        "attachments": attachments or [],
    }


def _reply_snapshots(db: Session, reply_ids: list[UUID]) -> dict[UUID, dict[str, object]]:
    if not reply_ids:
        return {}
    rows = (
        db.execute(
            select(
                messages.c.id,
                messages.c.encrypted_body,
                users.c.username,
            )
            .select_from(messages.outerjoin(users, messages.c.sender_id == users.c.id))
            .where(messages.c.id.in_(reply_ids))
        )
        .mappings()
        .all()
    )
    snapshots: dict[UUID, dict[str, object]] = {}
    for row in rows:
        try:
            plaintext = decrypt_message(row["encrypted_body"])
        except InvalidToken:
            plaintext = ""
        snapshots[row["id"]] = {
            "author": row["username"] or "unknown",
            "preview": plaintext.strip()[:140],
        }
    return snapshots


def _require_own_message(db: Session, conversation_id: UUID, message_id: UUID, current_user_id: UUID):
    row = (
        db.execute(
            select(messages).where(
                messages.c.id == message_id,
                messages.c.conversation_id == conversation_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if row["sender_id"] != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only change your own message"
        )
    return row


def get_total_unread_message_count(db: Session, current_user_id: UUID) -> int:
    conversation_rows = db.execute(
        select(conversations.c.id, conversation_members.c.last_read_at)
        .select_from(
            conversations.join(
                conversation_members,
                conversations.c.id == conversation_members.c.conversation_id,
            )
        )
        .where(
            conversation_members.c.user_id == current_user_id,
            conversation_members.c.hidden_at.is_(None),
        )
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
    attachment: PreparedAttachment | None = None,
    attachments: list[PreparedAttachment] | None = None,
    reply_to_id: UUID | None = None,
) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    if reply_to_id is not None:
        parent = db.execute(
            select(messages.c.id).where(
                messages.c.id == reply_to_id,
                messages.c.conversation_id == conversation_id,
            )
        ).first()
        if parent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reply target not found")

    prepared_attachments = list(attachments or [])
    if attachment is not None:
        prepared_attachments.append(attachment)
    plaintext = body.strip()
    if not plaintext and not prepared_attachments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message body is required"
        )

    encrypted = encrypt_message(plaintext)
    now = datetime.now(UTC)
    stored_attachments: list[dict[str, object]] = []

    try:
        created = (
            db.execute(
                insert(messages)
                .values(
                    conversation_id=conversation_id,
                    sender_id=current_user_id,
                    encrypted_body=encrypted,
                    encryption_version=1,
                    reply_to_id=reply_to_id,
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

        for item in prepared_attachments:
            storage_key = str(uuid4())
            PostgresBlobStore(db).put(storage_key, encrypt_bytes(item.data))
            attachment_row = (
                db.execute(
                    insert(message_attachments)
                    .values(
                        message_id=created["id"],
                        kind=item.kind,
                        filename=item.filename,
                        content_type=item.content_type,
                        byte_size=len(item.data),
                        storage_key=storage_key,
                    )
                    .returning(
                        message_attachments.c.id,
                        message_attachments.c.kind,
                        message_attachments.c.filename,
                        message_attachments.c.content_type,
                        message_attachments.c.byte_size,
                    )
                )
                .mappings()
                .one()
            )
            stored_attachments.append(dict(attachment_row))

        db.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(last_message_at=now)
        )
        db.execute(
            update(conversation_members)
            .where(conversation_members.c.conversation_id == conversation_id)
            .values(hidden_at=None)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not send message"
        ) from exc

    reply = _reply_snapshots(db, [reply_to_id] if reply_to_id else []).get(reply_to_id) if reply_to_id else None
    return {
        "message": _serialize_message(
            created, plaintext, attachments=stored_attachments, reply=reply
        )
    }


def get_messages_for_conversation(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, object]:
    conversation = _get_conversation_row(db, conversation_id)
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
    message_ids = [row["id"] for row in rows]
    reports_by_id = load_active_reports_for_targets(
        db,
        target_type="message",
        target_ids=message_ids,
        current_user_id=current_user_id,
    )
    attachment_rows = attachments_by_message(db, message_ids)
    replies = _reply_snapshots(
        db, [row["reply_to_id"] for row in rows if row.get("reply_to_id")]
    )
    for row in rows:
        try:
            plaintext = decrypt_message(row["encrypted_body"])
        except InvalidToken as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored message could not be decrypted",
            ) from exc
        items.append(
            _serialize_message(
                row,
                plaintext,
                report=reports_by_id.get(row["id"]),
                attachments=attachment_rows.get(row["id"], []),
                reply=replies.get(row["reply_to_id"]) if row.get("reply_to_id") else None,
            )
        )

    return {
        "conversation_id": conversation_id,
        "total": int(total or 0),
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
        "pins": list_pins(db, conversation_id),
        "can_pin": viewer_can_pin(conversation, current_user_id),
    }


def edit_message(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    body: str,
) -> dict[str, object]:
    _ensure_member(db, conversation_id, current_user_id)
    row = _require_own_message(db, conversation_id, message_id, current_user_id)
    plaintext = body.strip()
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message body is required"
        )
    now = datetime.now(UTC)
    encrypted = encrypt_message(plaintext)
    updated = (
        db.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(encrypted_body=encrypted, edited_at=now, updated_at=now)
            .returning(messages)
        )
        .mappings()
        .one()
    )
    db.commit()
    reply = None
    if updated.get("reply_to_id"):
        reply = _reply_snapshots(db, [updated["reply_to_id"]]).get(updated["reply_to_id"])
    return {"message": _serialize_message(updated, plaintext, reply=reply)}


def delete_message(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
) -> dict[str, object]:
    _ensure_member(db, conversation_id, current_user_id)
    _require_own_message(db, conversation_id, message_id, current_user_id)
    db.execute(delete(conversation_pins).where(conversation_pins.c.message_id == message_id))
    db.execute(delete(messages).where(messages.c.id == message_id))
    db.commit()
    return {"ok": True}


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
