from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Boolean, DateTime, Integer, String, and_, cast, func, literal, null, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    event_tags,
    event_updates,
    events,
    help_request_tags,
    help_requests,
    posts,
    project_tags,
    project_updates,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    user_follows,
    users,
    user_settings,
)

from app.services.access_control import (
    assert_can_view_scope,
    closed_community_only_tag_condition,
)
from app.services.projects_phases import display_stage_label as project_display_stage_label
from app.services.content import _help_request_role_summaries, _load_help_request_roles

VALID_SORTS = frozenset({"popular", "recent"})

EVENT_STAGE_LABEL_BY_PHASE_ID = {
    "proposal": "Proposal",
    "event-plan": "Event Plan",
    "activity": "Activity",
    "closed": "Closed",
}

_ZERO_INT = literal(0, Integer)
_EMPTY_ROLES = cast(literal("[]"), JSONB)



def _get_user_scope_ids(db: Session, user_id: UUID) -> tuple[list[UUID], list[UUID]]:
    rows = db.execute(
        select(scope_memberships.c.scope_kind, scope_memberships.c.scope_id)
        .where(
            scope_memberships.c.user_id == user_id,
            scope_memberships.c.scope_id.is_not(None),
        )
    ).all()
    channel_ids = [row[1] for row in rows if row[0] == "channel"]
    community_ids = [row[1] for row in rows if row[0] == "community"]
    return channel_ids, community_ids


def _get_followed_user_ids(db: Session, current_user_id: UUID) -> list[UUID]:
    rows = db.execute(
        select(user_follows.c.followed_id)
        .where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
    ).all()
    return [row[0] for row in rows]
