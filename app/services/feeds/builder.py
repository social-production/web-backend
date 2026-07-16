from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Integer,
    cast,
    literal,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.services.content import _load_help_request_roles
from app.services.feeds.selects import (
    _events_select,
    _help_requests_select,
    _projects_select,
    _threads_select,
)
from app.services.feeds.serializers import (
    _fetch_active_votes_for_rows,
    _fetch_latest_updates_for_items,
    _fetch_tags_for_items,
    _serialize_item,
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


def _build_feed(
    db: Session,
    sort: str,
    limit: int,
    offset: int,
    channel_ids: list[UUID] | None = None,
    community_ids: list[UUID] | None = None,
    current_user_id: UUID | None = None,
    *,
    public_only: bool = False,
) -> dict[str, object]:
    p_q = _projects_select(channel_ids, community_ids, public_only=public_only)
    t_q = _threads_select(channel_ids, community_ids, public_only=public_only)
    e_q = _events_select(channel_ids, community_ids, public_only=public_only)
    h_q = _help_requests_select(channel_ids, community_ids, public_only=public_only)

    parts = [q for q in (p_q, t_q, e_q, h_q) if q is not None]

    # No memberships means nothing to show in the home feed.
    if not parts:
        return {"total": 0, "sort": sort, "limit": limit, "offset": offset, "items": []}

    combined = union_all(*parts).subquery("feed")

    if sort == "popular":
        sort_col = (
            combined.c.signal_count
            + combined.c.vote_count
            + combined.c.comment_count
            + combined.c.member_count
            + combined.c.going_count
        ).desc()
    else:
        sort_col = combined.c.last_activity_at.desc()

    stmt = (
        select(combined)
        .order_by(sort_col, combined.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(stmt).mappings().all()
    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    help_roles_by_id = _load_help_request_roles(db, help_request_ids, current_user_id)
    items = [
        _serialize_item(
            row,
            tags,
            active_votes,
            updates,
            help_request_roles=help_roles_by_id.get(str(row["id"]))
            if row["entity_type"] == "help_request"
            else None,
        )
        for row in rows
    ]
    return {"total": len(items), "sort": sort, "limit": limit, "offset": offset, "items": items}
