from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Integer,
    cast,
    literal,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    scope_memberships,
    user_follows,
)

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
        select(scope_memberships.c.scope_kind, scope_memberships.c.scope_id).where(
            scope_memberships.c.user_id == user_id,
            scope_memberships.c.scope_id.is_not(None),
        )
    ).all()
    channel_ids = [row[1] for row in rows if row[0] == "channel"]
    community_ids = [row[1] for row in rows if row[0] == "community"]
    return channel_ids, community_ids


def _get_followed_user_ids(db: Session, current_user_id: UUID) -> list[UUID]:
    rows = db.execute(
        select(user_follows.c.followed_id).where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
    ).all()
    return [row[0] for row in rows]
