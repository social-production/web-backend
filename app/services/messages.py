from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto.messages import decrypt_message, encrypt_message
from app.models import (
    comments,
    conversation_members,
    conversations,
    event_memberships,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_requests,
    messages,
    project_memberships,
    projects,
    subject_chat_reads,
    users,
)


def _serialize_conversation(
    row: Mapping[str, object],
    participants: list[dict[str, object]],
    *,
    preview: str = "",
    unread_count: int = 0,
) -> dict[str, object]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_message_at": row["last_message_at"],
        "preview": preview,
        "unread_count": unread_count,
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


def _conversation_preview(db: Session, conversation_id: UUID) -> str:
    last_message = db.execute(
        select(messages.c.encrypted_body)
        .where(messages.c.conversation_id == conversation_id)
        .order_by(messages.c.created_at.desc())
        .limit(1)
    ).mappings().first()
    if last_message is None:
        return ""

    try:
        return decrypt_message(last_message["encrypted_body"])[:200]
    except InvalidToken:
        return ""


def _conversation_unread_count(
    db: Session,
    conversation_id: UUID,
    current_user_id: UUID,
    last_read_at: datetime | None,
) -> int:
    conditions = [
        messages.c.conversation_id == conversation_id,
        messages.c.sender_id != current_user_id,
    ]
    if last_read_at is None:
        conditions.append(messages.c.created_at.is_not(None))
    else:
        conditions.append(messages.c.created_at > last_read_at)

    count = db.execute(select(func.count()).select_from(messages).where(*conditions)).scalar_one()
    return int(count or 0)


def _get_subject_chat_last_read_at(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
) -> datetime | None:
    row = db.execute(
        select(subject_chat_reads.c.last_read_at).where(
            subject_chat_reads.c.user_id == current_user_id,
            subject_chat_reads.c.subject_type == subject_type,
            subject_chat_reads.c.subject_id == subject_id,
        )
    ).first()
    return row[0] if row else None


def _linked_chat_unread_count(
    db: Session,
    subject_type: str,
    subject_id: UUID,
    current_user_id: UUID,
    last_read_at: datetime | None,
) -> int:
    conditions = [
        comments.c.subject_type == subject_type,
        comments.c.subject_id == subject_id,
        comments.c.author_id != current_user_id,
    ]
    if last_read_at is not None:
        conditions.append(comments.c.created_at > last_read_at)

    count = db.execute(select(func.count()).select_from(comments).where(*conditions)).scalar_one()
    return int(count or 0)


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


def _get_conversation_participants(db: Session, conversation_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(users.c.id, users.c.username, users.c.profile_image_url)
        .select_from(
            conversation_members.join(users, conversation_members.c.user_id == users.c.id)
        )
        .where(conversation_members.c.conversation_id == conversation_id)
        .order_by(users.c.username.asc())
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]


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


def find_direct_conversation_between(
    db: Session,
    user_a: UUID,
    user_b: UUID,
) -> Mapping[str, object] | None:
    return _find_existing_direct_conversation(db, user_a, user_b)


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
        select(conversations, conversation_members.c.last_read_at)
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
        conversation_id = row["id"]
        participants = _get_conversation_participants(db, conversation_id)
        preview = _conversation_preview(db, conversation_id)
        unread_count = _conversation_unread_count(
            db,
            conversation_id,
            current_user_id,
            row["last_read_at"],
        )
        items.append(
            _serialize_conversation(
                row,
                participants,
                preview=preview,
                unread_count=unread_count,
            )
        )

    return {"total": len(items), "items": items}


def search_message_contacts(
    db: Session,
    current_user_id: UUID,
    query: str = "",
    limit: int = 8,
) -> dict[str, object]:
    normalized_query = query.strip().lower()
    capped_limit = max(1, min(limit, 25))
    conditions = [
        users.c.is_active.is_(True),
        users.c.id != current_user_id,
    ]
    if normalized_query:
        conditions.append(
            or_(
                users.c.username.ilike(f"%{normalized_query}%"),
            )
        )

    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
        .where(*conditions)
        .order_by(users.c.username.asc())
        .limit(capped_limit)
    ).mappings().all()

    return {
        "total": len(rows),
        "items": [
            {
                "id": row["id"],
                "username": row["username"],
                "bio": row["bio"],
                "profileImageUrl": row["profile_image_url"],
            }
            for row in rows
        ],
    }


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

    try:
        db.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(title=normalized_title)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not rename conversation") from exc

    refreshed = _get_conversation_row(db, conversation_id)
    participants = _get_conversation_participants(db, conversation_id)
    return {"conversation": _serialize_conversation(refreshed, participants)}


def get_linked_chats(db: Session, current_user_id: UUID) -> dict[str, object]:
    event_ids = {
        row[0]
        for row in db.execute(
            select(event_memberships.c.event_id).where(event_memberships.c.user_id == current_user_id)
        ).all()
    }
    project_ids = {
        row[0]
        for row in db.execute(
            select(project_memberships.c.project_id).where(project_memberships.c.user_id == current_user_id)
        ).all()
    }

    for subject_type, target_ids in (("event", event_ids), ("project", project_ids)):
        commented_ids = db.execute(
            select(comments.c.subject_id)
            .where(
                comments.c.subject_type == subject_type,
                comments.c.author_id == current_user_id,
            )
            .distinct()
        ).scalars().all()
        target_ids.update(commented_ids)

    items = []

    if event_ids:
        event_rows = db.execute(
            select(
                events.c.id,
                events.c.slug,
                events.c.title,
                events.c.last_activity_at,
                events.c.comment_count,
            )
            .where(events.c.id.in_(list(event_ids)))
            .order_by(events.c.last_activity_at.desc())
        ).mappings().all()

        for row in event_rows:
            last_comment = db.execute(
                select(comments.c.body, comments.c.created_at)
                .where(
                    comments.c.subject_type == "event",
                    comments.c.subject_id == row["id"],
                )
                .order_by(comments.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            last_read_at = _get_subject_chat_last_read_at(db, current_user_id, "event", row["id"])
            unread_count = _linked_chat_unread_count(
                db,
                "event",
                row["id"],
                current_user_id,
                last_read_at,
            )

            items.append({
                "id": str(row["id"]),
                "kind": "event",
                "entity_id": str(row["id"]),
                "entity_slug": row["slug"],
                "title": row["title"],
                "preview": last_comment["body"][:200] if last_comment else "",
                "last_message_at": _iso(last_comment["created_at"]) if last_comment else _iso(row["last_activity_at"]),
                "comment_count": row["comment_count"],
                "unread_count": unread_count,
            })

    if project_ids:
        project_rows = db.execute(
            select(
                projects.c.id,
                projects.c.slug,
                projects.c.title,
                projects.c.last_activity_at,
                projects.c.comment_count,
            )
            .where(projects.c.id.in_(list(project_ids)))
            .order_by(projects.c.last_activity_at.desc())
        ).mappings().all()

        for row in project_rows:
            last_comment = db.execute(
                select(comments.c.body, comments.c.created_at)
                .where(
                    comments.c.subject_type == "project",
                    comments.c.subject_id == row["id"],
                )
                .order_by(comments.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            last_read_at = _get_subject_chat_last_read_at(db, current_user_id, "project", row["id"])
            unread_count = _linked_chat_unread_count(
                db,
                "project",
                row["id"],
                current_user_id,
                last_read_at,
            )

            items.append({
                "id": str(row["id"]),
                "kind": "project",
                "entity_id": str(row["id"]),
                "entity_slug": row["slug"],
                "title": row["title"],
                "preview": last_comment["body"][:200] if last_comment else "",
                "last_message_at": _iso(last_comment["created_at"]) if last_comment else _iso(row["last_activity_at"]),
                "comment_count": row["comment_count"],
                "unread_count": unread_count,
            })

    help_request_ids: set[UUID] = set()
    help_request_ids.update(
        db.execute(
            select(help_requests.c.id).where(help_requests.c.author_id == current_user_id)
        ).scalars().all()
    )
    help_request_ids.update(
        db.execute(
            select(help_request_roles.c.help_request_id)
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                )
            )
            .where(help_request_role_assignments.c.user_id == current_user_id)
            .distinct()
        ).scalars().all()
    )
    help_request_ids.update(
        db.execute(
            select(comments.c.subject_id)
            .where(
                comments.c.subject_type == "help_request",
                comments.c.author_id == current_user_id,
            )
            .distinct()
        ).scalars().all()
    )

    if help_request_ids:
        help_request_rows = db.execute(
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.comment_count,
                help_requests.c.created_at,
            )
            .where(help_requests.c.id.in_(list(help_request_ids)))
        ).mappings().all()

        for row in help_request_rows:
            last_comment = db.execute(
                select(comments.c.body, comments.c.created_at)
                .where(
                    comments.c.subject_type == "help_request",
                    comments.c.subject_id == row["id"],
                )
                .order_by(comments.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            last_read_at = _get_subject_chat_last_read_at(db, current_user_id, "help_request", row["id"])
            unread_count = _linked_chat_unread_count(
                db,
                "help_request",
                row["id"],
                current_user_id,
                last_read_at,
            )

            items.append({
                "id": str(row["id"]),
                "kind": "help_request",
                "entity_id": str(row["id"]),
                "entity_slug": str(row["id"]),
                "title": row["title"],
                "preview": last_comment["body"][:200] if last_comment else "",
                "last_message_at": _iso(last_comment["created_at"]) if last_comment else _iso(row["created_at"]),
                "comment_count": row["comment_count"],
                "unread_count": unread_count,
            })

    items.sort(key=lambda x: x["last_message_at"], reverse=True)
    return {"total": len(items), "items": items}


def mark_linked_chat_read(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in {"project", "event", "help_request"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject_type must be project, event, or help_request",
        )

    if normalized_subject_type == "project":
        exists = db.execute(select(projects.c.id).where(projects.c.id == subject_id)).first()
    elif normalized_subject_type == "event":
        exists = db.execute(select(events.c.id).where(events.c.id == subject_id)).first()
    else:
        exists = db.execute(select(help_requests.c.id).where(help_requests.c.id == subject_id)).first()

    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{normalized_subject_type.capitalize()} not found")

    now = datetime.now(timezone.utc)
    existing = db.execute(
        select(subject_chat_reads.c.user_id).where(
            subject_chat_reads.c.user_id == current_user_id,
            subject_chat_reads.c.subject_type == normalized_subject_type,
            subject_chat_reads.c.subject_id == subject_id,
        )
    ).first()

    if existing is None:
        db.execute(
            insert(subject_chat_reads).values(
                user_id=current_user_id,
                subject_type=normalized_subject_type,
                subject_id=subject_id,
                last_read_at=now,
            )
        )
    else:
        db.execute(
            update(subject_chat_reads)
            .where(
                subject_chat_reads.c.user_id == current_user_id,
                subject_chat_reads.c.subject_type == normalized_subject_type,
                subject_chat_reads.c.subject_id == subject_id,
            )
            .values(last_read_at=now)
        )

    db.commit()
    return {
        "ok": True,
        "subject_type": normalized_subject_type,
        "subject_id": subject_id,
        "last_read_at": now,
    }


def _iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


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

    try:
        db.execute(
            delete(conversation_members).where(
                conversation_members.c.conversation_id == conversation_id,
                conversation_members.c.user_id == target_user_id,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not remove group member") from exc

    refreshed = _get_conversation_row(db, conversation_id)
    participants = _get_conversation_participants(db, conversation_id)
    return {"conversation": _serialize_conversation(refreshed, participants)}


def mark_conversation_as_read(
    db: Session,
    current_user_id: UUID,
    conversation_id: UUID,
) -> dict[str, object]:
    _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)

    now = datetime.now(timezone.utc)
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