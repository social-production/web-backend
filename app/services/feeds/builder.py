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
from app.services.feeds.location_display import apply_feed_location_labels
from app.services.feeds.ranking import (
    apply_schedule_window_filter,
    filter_entity_type,
    sort_order_columns,
)
from app.services.feeds.selects import (
    _events_select,
    _help_requests_select,
    _projects_select,
    _threads_select,
)
from app.services.feeds.serializers import (
    _fetch_active_reports,
    _fetch_active_votes_for_rows,
    _fetch_latest_updates_for_items,
    _fetch_tags_for_items,
    _fetch_viewer_signals_for_rows,
    _serialize_item,
)

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
    window: str = "all",
    entity_filter: str = "all",
    timezone_name: str | None = None,
) -> dict[str, object]:
    selects_by_type = {
        "project": _projects_select(channel_ids, community_ids, public_only=public_only),
        "thread": _threads_select(channel_ids, community_ids, public_only=public_only),
        "event": _events_select(channel_ids, community_ids, public_only=public_only),
        "help_request": _help_requests_select(channel_ids, community_ids, public_only=public_only),
    }

    wanted_type = filter_entity_type(entity_filter)
    if wanted_type is not None:
        selects_by_type = {
            entity_type: q
            for entity_type, q in selects_by_type.items()
            if entity_type == wanted_type
        }

    parts = [q for q in selects_by_type.values() if q is not None]

    # No memberships means nothing to show in the home feed.
    if not parts:
        return {
            "total": 0,
            "sort": sort,
            "window": window,
            "filter": entity_filter,
            "limit": limit,
            "offset": offset,
            "items": [],
        }

    combined = union_all(*parts).subquery("feed")

    stmt = select(combined)
    stmt = apply_schedule_window_filter(stmt, combined, window, timezone_name)
    stmt = stmt.order_by(*sort_order_columns(combined, sort)).limit(limit).offset(offset)

    rows = db.execute(stmt).mappings().all()
    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    viewer_signals = _fetch_viewer_signals_for_rows(db, rows, current_user_id)
    active_reports = _fetch_active_reports(db, rows, current_user_id)
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
            viewer_signals=viewer_signals,
            active_reports=active_reports,
        )
        for row in rows
    ]
    items = apply_feed_location_labels(db, items)
    return {
        "total": len(items),
        "sort": sort,
        "window": window,
        "filter": entity_filter,
        "limit": limit,
        "offset": offset,
        "items": items,
    }
