from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto.messages import decrypt_message, encrypt_message
from app.models import conversation_members, conversations, messages, users


def _serialize_conversation(row: Mapping[str, object], participants: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_message_at": row["last_message_at"],
        "participants": participants,
    }


def _serialize_message(row: Mapping[str, object], body: str) -> dict[str, object]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "body": body,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_user_by_username(db: Session, username: str) -> Mapping[str, object]:
    row = db.execute(
        select(users.c.id, users.c.username)
        .where(users.c.username == username.strip().lower())
        .limit(1)
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User not found: {username}")
    return row


def _get_conversation_row(db: Session, conversation_id: UUID) -> Mapping[str, object]:
    row = db.execute(
        select(conversations).where(conversations.c.id == conversation_id)
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return row


def _ensure_member(db: Session, conversation_id: UUID, user_id: UUID) -> None:
    member = db.execute(
        select(conversation_members.c.user_id)
        .where(
            conversation_members.c.conversation_id == conversation_id,
            conversation_members.c.user_id == user_id,
        )
        .limit(1)
    ).first()
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of this conversation")


def _get_conversation_participants(db: Session, conversation_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(users.c.id, users.c.username)
        .select_from(
            conversation_members.join(users, conversation_members.c.user_id == users.c.id)
        )
        .where(conversation_members.c.conversation_id == conversation_id)
        .order_by(users.c.username.asc())
    ).mappings().all()
    return [{"id": row["id"], "username": row["username"]} for row in rows]


def _ensure_group_manager(db: Session, conversation_row: Mapping[str, object], current_user_id: UUID) -> None:
    if conversation_row["kind"] != "group":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Conversation is not a group")
    if conversation_row["created_by"] != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the group creator can manage members")


def _find_existing_direct_conversation(db: Session, user_a: UUID, user_b: UUID) -> Mapping[str, object] | None:
    candidate_rows = db.execute(
        select(conversations)
        .select_from(
            conversations.join(
                conversation_members,
                conversations.c.id == conversation_members.c.conversation_id,
            )
        )
        .where(
            conversations.c.kind == "direct",
            conversation_members.c.user_id == user_a,
        )
        .order_by(conversations.c.created_at.desc())
    ).mappings().all()

    for row in candidate_rows:
        member_ids = set(
            db.execute(
                select(conversation_members.c.user_id).where(
                    conversation_members.c.conversation_id == row["id"]
                )
            ).scalars().all()
        )
        if member_ids == {user_a, user_b}:
            return row

    return None


def start_direct_conversation(db: Session, current_user_id: UUID, other_username: str) -> dict[str, object]:
    other_user = _get_user_by_username(db, other_username)
    other_user_id = other_user["id"]
    if other_user_id == current_user_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot start a direct message with yourself")

    existing = _find_existing_direct_conversation(db, current_user_id, other_user_id)
    if existing is not None:
        participants = _get_conversation_participants(db, existing["id"])
        return {"conversation": _serialize_conversation(existing, participants)}

    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(conversations)
            .values(kind="direct", title=None, created_by=current_user_id, last_message_at=None)
            .returning(
                conversations.c.id,
                conversations.c.kind,
                conversations.c.title,
                conversations.c.created_by,
                conversations.c.created_at,
                conversations.c.updated_at,
                conversations.c.last_message_at,
            )
        ).mappings().one()

        db.execute(
            insert(conversation_members),
            [
                {
                    "conversation_id": created["id"],
                    "user_id": current_user_id,
                    "joined_at": now,
                    "last_read_at": None,
                },
                {
                    "conversation_id": created["id"],
                    "user_id": other_user_id,
                    "joined_at": now,
                    "last_read_at": None,
                },
            ],
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start direct conversation",
        ) from exc

    participants = _get_conversation_participants(db, created["id"])
    return {"conversation": _serialize_conversation(created, participants)}


def create_group_conversation(
    db: Session,
    current_user_id: UUID,
    title: str,
    participant_usernames: list[str],
) -> dict[str, object]:
    normalized = []
    seen = set()
    for username in participant_usernames:
        value = username.strip().lower()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)

    other_ids: list[UUID] = []
    for username in normalized:
        row = _get_user_by_username(db, username)
        if row["id"] != current_user_id:
            other_ids.append(row["id"])

    member_ids = [current_user_id, *other_ids]
    if len(member_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Group conversation requires at least one other user",
        )

    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(conversations)
            .values(kind="group", title=title.strip(), created_by=current_user_id, last_message_at=None)
            .returning(
                conversations.c.id,
                conversations.c.kind,
                conversations.c.title,
                conversations.c.created_by,
                conversations.c.created_at,
                conversations.c.updated_at,
                conversations.c.last_message_at,
            )
        ).mappings().one()

        db.execute(
            insert(conversation_members),
            [
                {
                    "conversation_id": created["id"],
                    "user_id": member_id,
                    "joined_at": now,
                    "last_read_at": None,
                }
                for member_id in member_ids
            ],
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create group conversation",
        ) from exc

    participants = _get_conversation_participants(db, created["id"])
    return {"conversation": _serialize_conversation(created, participants)}


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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message body is required")

    encrypted = encrypt_message(plaintext)
    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
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
        ).mappings().one()

        db.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(last_message_at=now)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not send message") from exc

    return {"message": _serialize_message(created, plaintext)}


def list_conversations(db: Session, current_user_id: UUID) -> dict[str, object]:
    rows = db.execute(
        select(conversations)
        .select_from(
            conversations.join(
                conversation_members,
                conversations.c.id == conversation_members.c.conversation_id,
            )
        )
        .where(conversation_members.c.user_id == current_user_id)
        .order_by(conversations.c.last_message_at.desc().nullslast(), conversations.c.created_at.desc())
    ).mappings().all()

    items = []
    for row in rows:
        participants = _get_conversation_participants(db, row["id"])
        items.append(_serialize_conversation(row, participants))

    return {"total": len(items), "items": items}


def get_messages_for_conversation(db: Session, current_user_id: UUID, conversation_id: UUID) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)

    rows = db.execute(
        select(messages)
        .where(messages.c.conversation_id == conversation_id)
        .order_by(messages.c.created_at.asc())
    ).mappings().all()

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

    return {"conversation_id": conversation_id, "total": len(items), "items": items}


def rename_group_conversation(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    title: str,
) -> dict[str, object]:
    conversation_row = _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    _ensure_group_manager(db, conversation_row, current_user_id)

    normalized_title = title.strip()
    if not normalized_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title is required")

    db.execute(
        update(conversations)
        .where(conversations.c.id == conversation_id)
        .values(title=normalized_title)
    )
    db.commit()

    refreshed = _get_conversation_row(db, conversation_id)
    participants = _get_conversation_participants(db, conversation_id)
    return {"conversation": _serialize_conversation(refreshed, participants)}


def add_group_member(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    username: str,
) -> dict[str, object]:
    conversation_row = _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    _ensure_group_manager(db, conversation_row, current_user_id)

    user_row = _get_user_by_username(db, username)
    target_user_id = user_row["id"]

    try:
        db.execute(
            insert(conversation_members).values(
                conversation_id=conversation_id,
                user_id=target_user_id,
                joined_at=datetime.now(timezone.utc),
                last_read_at=None,
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()

    refreshed = _get_conversation_row(db, conversation_id)
    participants = _get_conversation_participants(db, conversation_id)
    return {"conversation": _serialize_conversation(refreshed, participants)}


def remove_group_member(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
    username: str,
) -> dict[str, object]:
    conversation_row = _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    _ensure_group_manager(db, conversation_row, current_user_id)

    user_row = _get_user_by_username(db, username)
    target_user_id = user_row["id"]
    if target_user_id == current_user_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Creator cannot remove self")

    db.execute(
        delete(conversation_members).where(
            conversation_members.c.conversation_id == conversation_id,
            conversation_members.c.user_id == target_user_id,
        )
    )
    db.commit()

    refreshed = _get_conversation_row(db, conversation_id)
    participants = _get_conversation_participants(db, conversation_id)
    return {"conversation": _serialize_conversation(refreshed, participants)}