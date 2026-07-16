from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    notifications,
    users,
)
from app.services.messages import get_total_unread_message_count


def _get_viewer_row(db: Session, current_user_id: UUID):
    row = (
        db.execute(
            select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
                users.c.id == current_user_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viewer not found")
    return row


def _get_unread_notification_count(db: Session, current_user_id: UUID) -> int:
    count = db.execute(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.recipient_id == current_user_id,
            notifications.c.is_unread.is_(True),
        )
    ).scalar_one()
    return int(count or 0)


def _get_unread_message_count(db: Session, current_user_id: UUID) -> int:
    return get_total_unread_message_count(db, current_user_id)


def _small_iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def get_bootstrap_summary(db: Session, current_user_id: UUID | None) -> dict[str, object]:
    if current_user_id is None:
        return {
            "unreadCounts": {
                "notifications": 0,
                "messages": 0,
            },
        }

    return {
        "unreadCounts": {
            "notifications": _get_unread_notification_count(db, current_user_id),
            "messages": _get_unread_message_count(db, current_user_id),
        },
    }
