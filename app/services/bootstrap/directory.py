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



def _get_platform_directory_item(db: Session, current_user_id: UUID | None) -> dict[str, object] | None:
    row = db.execute(
        select(channels.c.id, channels.c.slug, channels.c.name)
        .where(channels.c.slug.in_(["platform", "stewardship"]))
        .order_by(channels.c.slug.asc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return None

    membership = None
    if current_user_id is not None:
        membership = db.execute(
            select(scope_memberships.c.user_id).where(
                scope_memberships.c.scope_kind == "channel",
                scope_memberships.c.scope_id == row["id"],
                scope_memberships.c.user_id == current_user_id,
            )
        ).first()

    return {
        "slug": row["slug"],
        "label": row["name"],
        "href": "/platform",
        "visibility": "public",
        "viewerIsMember": membership is not None,
    }


def _get_channel_directory_items(db: Session, current_user_id: UUID | None) -> list[dict[str, object]]:
    if current_user_id is None:
        return []

    rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(
            scope_memberships.join(channels, channels.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "channel",
            channels.c.slug.not_in(["platform", "stewardship"]),
        )
        .order_by(channels.c.name.asc())
    ).mappings().all()

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/channels/{row['slug']}",
            "visibility": "public",
        }
        for row in rows
    ]


def _get_community_directory_items(db: Session, current_user_id: UUID | None) -> list[dict[str, object]]:
    if current_user_id is None:
        return []

    rows = db.execute(
        select(communities.c.slug, communities.c.name, communities.c.join_policy)
        .select_from(
            scope_memberships.join(communities, communities.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "community",
        )
        .order_by(communities.c.name.asc())
    ).mappings().all()

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/communities/{row['slug']}",
            "visibility": "private" if row["join_policy"] == "closed" else "public",
        }
        for row in rows
    ]


def _get_suggested_contacts(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    followed_subquery = (
        select(user_follows.c.followed_id)
        .where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
        .subquery("followed_users")
    )

    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
        .where(
            users.c.is_active.is_(True),
            users.c.id != current_user_id,
            not_(users.c.id.in_(select(followed_subquery.c.followed_id))),
        )
        .order_by(users.c.username.asc())
        .limit(8)
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "username": row["username"],
            "bio": row["bio"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]
