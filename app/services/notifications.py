from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import notifications, users


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize_notification(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "recipient_id": row["recipient_id"],
        "actor_id": row["actor_id"],
        "actor_username": row.get("actor_username"),
        "kind": row["kind"],
        "surface": row["surface"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "target_id": row["target_id"],
        "title": row["title"],
        "body": row["body"],
        "href": row["href"],
        "is_unread": row["is_unread"],
        "created_at": _iso(row["created_at"]),
        "read_at": _iso(row.get("read_at")),
    }


def create_notification(
    db: Session,
    recipient_id: UUID,
    kind: str,
    surface: str,
    subject_type: str,
    subject_id: UUID,
    title: str,
    body: str,
    href: str,
    actor_id: UUID | None = None,
    target_id: UUID | None = None,
) -> dict[str, object]:
    """Internal helper for other services to emit notifications."""
    try:
        created = db.execute(
            insert(notifications)
            .values(
                recipient_id=recipient_id,
                actor_id=actor_id,
                kind=kind.strip(),
                surface=surface.strip(),
                subject_type=subject_type.strip(),
                subject_id=subject_id,
                target_id=target_id,
                title=title.strip(),
                body=body.strip(),
                href=href.strip(),
                is_unread=True,
                read_at=None,
            )
            .returning(
                notifications.c.id,
                notifications.c.recipient_id,
                notifications.c.actor_id,
                notifications.c.kind,
                notifications.c.surface,
                notifications.c.subject_type,
                notifications.c.subject_id,
                notifications.c.target_id,
                notifications.c.title,
                notifications.c.body,
                notifications.c.href,
                notifications.c.is_unread,
                notifications.c.created_at,
                notifications.c.read_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create notification",
        ) from exc

    return {"notification": _serialize_notification(created)}


def list_notifications(
    db: Session,
    current_user_id: UUID,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    actor_users = users.alias("actor_users")
    query = (
        select(
            notifications,
            actor_users.c.username.label("actor_username"),
        )
        .select_from(notifications.outerjoin(actor_users, actor_users.c.id == notifications.c.actor_id))
        .where(notifications.c.recipient_id == current_user_id)
    )
    if unread_only:
        query = query.where(notifications.c.is_unread.is_(True))

    rows = db.execute(
        query.order_by(notifications.c.created_at.desc()).limit(limit).offset(offset)
    ).mappings().all()
    items = [_serialize_notification(row) for row in rows]

    return {
        "total": len(items),
        "items": items,
    }


def mark_notification_read(db: Session, current_user_id: UUID, notification_id: UUID) -> dict[str, object]:
    row = db.execute(
        select(notifications)
        .where(
            notifications.c.id == notification_id,
            notifications.c.recipient_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    if row["is_unread"]:
        db.execute(
            update(notifications)
            .where(notifications.c.id == notification_id)
            .values(is_unread=False, read_at=datetime.now(timezone.utc))
        )
        db.commit()

    refreshed = db.execute(
        select(notifications).where(notifications.c.id == notification_id)
    ).mappings().one()
    return {"notification": _serialize_notification(refreshed)}


def mark_all_notifications_read(db: Session, current_user_id: UUID) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(notifications)
        .where(
            notifications.c.recipient_id == current_user_id,
            notifications.c.is_unread.is_(True),
        )
        .values(is_unread=False, read_at=now)
    )
    db.commit()
    return {"ok": True, "updated": int(result.rowcount or 0)}