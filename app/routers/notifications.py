from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_notifications_provider
from app.ports import NotificationsProvider

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: UUID
    recipient_id: UUID
    actor_id: UUID | None = None
    actor_username: str | None = None
    actor_profile_image_url: str | None = None
    kind: str
    surface: str
    subject_type: str
    subject_id: UUID
    target_id: UUID | None = None
    title: str
    body: str
    href: str
    is_unread: bool
    created_at: datetime
    read_at: datetime | None = None


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
    notifications: NotificationsProvider = Depends(get_notifications_provider),
) -> dict[str, object]:
    return notifications.list_notifications(
        current_user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_one_read(
    notification_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    notifications: NotificationsProvider = Depends(get_notifications_provider),
) -> dict[str, object]:
    return notifications.mark_read(current_user_id, notification_id)


@router.patch("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(
    current_user_id: UUID = Depends(get_current_user_id),
    notifications: NotificationsProvider = Depends(get_notifications_provider),
) -> dict[str, object]:
    return notifications.mark_all_read(current_user_id)
