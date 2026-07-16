from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, literal, not_, or_, select, union_all
from sqlalchemy.orm import Session


from app.models import (
    channels,
    communities,
    conversation_members,
    conversations,
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_edit_request_votes,
    event_edit_requests,
    event_memberships,
    event_phase_change_votes,
    event_phase_change_requests,
    event_plan_votes,
    event_plan_criterion_ratings,
    event_plans,
    event_update_request_votes,
    event_update_requests,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_request_tags,
    help_requests,
    notifications,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_votes,
    project_plan_criterion_ratings,
    project_plans,
    project_service_requests,
    project_update_request_votes,
    project_update_requests,
    projects,
    scope_memberships,
    user_follows,
    users,
)
from app.services.content import _help_request_role_summaries, _load_help_request_roles
from app.services.feeds import _truncate_update_body
from app.services.messages import find_direct_conversation_between, get_total_unread_message_count



def _get_viewer_row(db: Session, current_user_id: UUID):
    row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
            users.c.id == current_user_id
        )
    ).mappings().first()
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
