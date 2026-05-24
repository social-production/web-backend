from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.notifications import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: UUID
    recipient_id: UUID
    actor_id: UUID | None = None
    kind: str
    surface: str
    subject_type: str
    subject_id: UUID
    target_id: UUID | None = None
    title: str
    body: str
    href: str
    is_unread: bool
    created_at: object
    read_at: object


class NotificationsListResponse(BaseModel):
    total: int
    items: list[NotificationOut]


class NotificationResponse(BaseModel):
    notification: NotificationOut


class MarkAllReadResponse(BaseModel):
    ok: bool
    updated: int


@router.get("", response_model=NotificationsListResponse)
def list_my_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_notifications(
        db=db,
        current_user_id=current_user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_one_read(
    notification_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return mark_notification_read(db=db, current_user_id=current_user_id, notification_id=notification_id)


@router.patch("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return mark_all_notifications_read(db=db, current_user_id=current_user_id)