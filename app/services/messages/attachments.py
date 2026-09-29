"""One or more photos or files per direct, group, or linked chat message."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.adapters.postgres.blob_store import PostgresBlobStore
from app.crypto.messages import decrypt_bytes, encrypt_bytes
from app.models import (
    comment_attachments,
    comments,
    conversation_members,
    conversation_pins,
    message_attachments,
    messages,
)
from app.services.access_control import assert_can_view_subject
from app.services.messages.conversations import (
    _ensure_member,
    _get_conversation_row,
    _message_preview,
)

MAX_IMAGE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PINS = 5
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")


@dataclass(frozen=True)
class PreparedAttachment:
    kind: str
    filename: str
    content_type: str
    data: bytes


def _safe_filename(name: str | None) -> str:
    raw = (name or "file").replace("\\", "/").split("/")[-1]
    raw = raw.replace("\x00", "").replace("\r", "").replace("\n", "").strip().strip('"')
    if not raw or raw in {".", ".."}:
        raw = "file"
    return raw[:200]


def _normalize_media_type(content_type: str | None) -> str:
    media = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    if media == "image/jpg":
        media = "image/jpeg"
    if not _MEDIA_TYPE.fullmatch(media):
        return "application/octet-stream"
    return media


def prepare_attachment(
    filename: str | None, content_type: str | None, data: bytes
) -> PreparedAttachment:
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The file is empty"
        )

    filename_value = _safe_filename(filename)
    media = _normalize_media_type(content_type)
    if media.startswith("video/") or filename_value.lower().endswith(VIDEO_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Videos are not supported yet",
        )

    if media in IMAGE_TYPES:
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Photos must be 1MB or smaller",
            )
        kind = "image"
    elif media.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use a JPEG, PNG, or WebP photo",
        )
    else:
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Files must be 10MB or smaller",
            )
        kind = "file"

    return PreparedAttachment(
        kind=kind,
        filename=filename_value,
        content_type=media,
        data=data,
    )


async def prepare_form_uploads(form: object) -> list[PreparedAttachment]:
    getlist = getattr(form, "getlist", None)
    uploads = [
        item for item in (getlist("file") if callable(getlist) else []) if hasattr(item, "read")
    ]
    if len(uploads) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"You can attach up to {MAX_ATTACHMENTS} files",
        )

    prepared: list[PreparedAttachment] = []
    for upload in uploads:
        data = await upload.read()
        prepared.append(
            prepare_attachment(
                getattr(upload, "filename", None),
                getattr(upload, "content_type", None),
                data,
            )
        )
    return prepared


def viewer_can_pin(conversation: Mapping[str, object], user_id: UUID) -> bool:
    kind = str(conversation["kind"])
    if kind == "direct":
        return True
    if kind == "group":
        return conversation["created_by"] == user_id
    return False


def _assert_can_pin(conversation: Mapping[str, object], user_id: UUID) -> None:
    if viewer_can_pin(conversation, user_id):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the group creator can pin messages",
    )


def attachments_by_message(
    db: Session, message_ids: list[UUID]
) -> dict[UUID, list[dict[str, object]]]:
    if not message_ids:
        return {}

    rows = (
        db.execute(
            select(message_attachments).where(message_attachments.c.message_id.in_(message_ids))
        )
        .mappings()
        .all()
    )
    grouped: dict[UUID, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(row["message_id"], []).append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "byte_size": row["byte_size"],
            }
        )
    return grouped


def list_pins(db: Session, conversation_id: UUID) -> list[dict[str, object]]:
    rows = (
        db.execute(
            select(
                conversation_pins.c.message_id,
                conversation_pins.c.pinned_at,
                messages.c.encrypted_body,
            )
            .select_from(
                conversation_pins.join(messages, conversation_pins.c.message_id == messages.c.id)
            )
            .where(conversation_pins.c.conversation_id == conversation_id)
            .order_by(conversation_pins.c.pinned_at.asc())
        )
        .mappings()
        .all()
    )
    return [
        {
            "message_id": row["message_id"],
            "pinned_at": row["pinned_at"],
            "preview": _message_preview(db, row["message_id"], row["encrypted_body"]),
        }
        for row in rows
    ]


def pin_message(
    db: Session, current_user_id: UUID, conversation_id: UUID, message_id: UUID
) -> dict[str, object]:
    conversation = _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    _assert_can_pin(conversation, current_user_id)

    message = db.execute(
        select(messages.c.id)
        .where(messages.c.id == message_id, messages.c.conversation_id == conversation_id)
        .limit(1)
    ).first()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    existing = db.execute(
        select(conversation_pins.c.message_id)
        .where(
            conversation_pins.c.conversation_id == conversation_id,
            conversation_pins.c.message_id == message_id,
        )
        .limit(1)
    ).first()
    if existing is not None:
        return {"ok": True}

    count = db.execute(
        select(func.count())
        .select_from(conversation_pins)
        .where(conversation_pins.c.conversation_id == conversation_id)
    ).scalar_one()
    if int(count or 0) >= MAX_PINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You can pin at most 5 messages",
        )

    db.execute(
        insert(conversation_pins).values(
            conversation_id=conversation_id,
            message_id=message_id,
            pinned_by=current_user_id,
        )
    )
    db.commit()
    return {"ok": True}


def unpin_message(
    db: Session, current_user_id: UUID, conversation_id: UUID, message_id: UUID
) -> dict[str, object]:
    conversation = _get_conversation_row(db, conversation_id)
    _ensure_member(db, conversation_id, current_user_id)
    _assert_can_pin(conversation, current_user_id)
    db.execute(
        delete(conversation_pins).where(
            conversation_pins.c.conversation_id == conversation_id,
            conversation_pins.c.message_id == message_id,
        )
    )
    db.commit()
    return {"ok": True}


def read_message_attachment(
    db: Session, current_user_id: UUID, attachment_id: UUID
) -> dict[str, object]:
    row = (
        db.execute(
            select(
                message_attachments.c.content_type,
                message_attachments.c.filename,
                message_attachments.c.kind,
                message_attachments.c.storage_key,
                messages.c.conversation_id,
            )
            .select_from(
                message_attachments.join(
                    messages, message_attachments.c.message_id == messages.c.id
                )
            )
            .where(message_attachments.c.id == attachment_id)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    membership = db.execute(
        select(conversation_members.c.user_id)
        .where(
            conversation_members.c.conversation_id == row["conversation_id"],
            conversation_members.c.user_id == current_user_id,
        )
        .limit(1)
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    ciphertext = PostgresBlobStore(db).get(str(row["storage_key"]))
    if ciphertext is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    try:
        data = decrypt_bytes(ciphertext)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored attachment could not be decrypted",
        ) from exc

    return {
        "data": data,
        "content_type": row["content_type"],
        "filename": row["filename"],
        "kind": row["kind"],
    }


LINKED_CHAT_SUBJECTS = frozenset({"project", "event", "help_request"})
MAX_ATTACHMENTS = 10


def _attachment_fields(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "byte_size": row["byte_size"],
    }


def store_comment_attachment(
    db: Session, comment_id: UUID, attachment: PreparedAttachment
) -> dict[str, object]:
    storage_key = str(uuid4())
    PostgresBlobStore(db).put(storage_key, encrypt_bytes(attachment.data))
    row = (
        db.execute(
            insert(comment_attachments)
            .values(
                comment_id=comment_id,
                kind=attachment.kind,
                filename=attachment.filename,
                content_type=attachment.content_type,
                byte_size=len(attachment.data),
                storage_key=storage_key,
            )
            .returning(
                comment_attachments.c.id,
                comment_attachments.c.kind,
                comment_attachments.c.filename,
                comment_attachments.c.content_type,
                comment_attachments.c.byte_size,
            )
        )
        .mappings()
        .one()
    )
    return _attachment_fields(row)


def attachments_for_comments(
    db: Session, comment_ids: list[UUID]
) -> dict[UUID, list[dict[str, object]]]:
    if not comment_ids:
        return {}
    rows = (
        db.execute(
            select(
                comment_attachments.c.comment_id,
                comment_attachments.c.id,
                comment_attachments.c.kind,
                comment_attachments.c.filename,
                comment_attachments.c.content_type,
                comment_attachments.c.byte_size,
            ).where(comment_attachments.c.comment_id.in_(comment_ids))
        )
        .mappings()
        .all()
    )
    grouped: dict[UUID, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(row["comment_id"], []).append(_attachment_fields(row))
    return grouped


def detail_attachments_by_comment(
    db: Session, comment_ids: list[UUID]
) -> dict[str, list[dict[str, object]]]:
    grouped = attachments_for_comments(db, comment_ids)
    return {
        str(comment_id): [
            {
                "id": str(item["id"]),
                "kind": item["kind"],
                "filename": item["filename"],
                "contentType": item["content_type"],
                "byteSize": item["byte_size"],
                "url": "",
            }
            for item in items
        ]
        for comment_id, items in grouped.items()
    }


def read_comment_attachment(
    db: Session, current_user_id: UUID, attachment_id: UUID
) -> dict[str, object]:
    row = (
        db.execute(
            select(
                comment_attachments.c.kind,
                comment_attachments.c.filename,
                comment_attachments.c.content_type,
                comment_attachments.c.storage_key,
                comments.c.subject_type,
                comments.c.subject_id,
            )
            .select_from(
                comment_attachments.join(
                    comments, comment_attachments.c.comment_id == comments.c.id
                )
            )
            .where(comment_attachments.c.id == attachment_id)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    try:
        assert_can_view_subject(db, current_user_id, str(row["subject_type"]), row["subject_id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found"
        ) from exc

    ciphertext = PostgresBlobStore(db).get(str(row["storage_key"]))
    if ciphertext is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    try:
        data = decrypt_bytes(ciphertext)
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored attachment could not be decrypted",
        ) from exc

    return {
        "data": data,
        "content_type": row["content_type"],
        "filename": row["filename"],
        "kind": row["kind"],
    }
