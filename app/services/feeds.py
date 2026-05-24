from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Integer, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    event_tags,
    events,
    project_tags,
    projects,
    scope_memberships,
    thread_tags,
    threads,
)

VALID_SORTS = frozenset({"popular", "recent"})

_ZERO_INT = literal(0, Integer)


def _projects_select(channel_ids: list[UUID] | None, community_ids: list[UUID] | None):
    q = select(
        projects.c.id,
        literal("project").label("entity_type"),
        projects.c.slug,
        projects.c.title,
        projects.c.description.label("body"),
        projects.c.author_id,
        projects.c.signal_count,
        projects.c.vote_count,
        projects.c.comment_count,
        projects.c.member_count,
        _ZERO_INT.label("going_count"),
        projects.c.last_activity_at,
        projects.c.created_at,
    ).where(projects.c.is_closed.is_(False))

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(project_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(project_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(project_tags, project_tags.c.project_id == projects.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    return q


def _threads_select(channel_ids: list[UUID] | None, community_ids: list[UUID] | None):
    q = select(
        threads.c.id,
        literal("thread").label("entity_type"),
        threads.c.slug,
        threads.c.title,
        threads.c.body,
        threads.c.author_id,
        _ZERO_INT.label("signal_count"),
        threads.c.vote_count,
        threads.c.comment_count,
        _ZERO_INT.label("member_count"),
        _ZERO_INT.label("going_count"),
        threads.c.last_activity_at,
        threads.c.created_at,
    )

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(thread_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(thread_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(thread_tags, thread_tags.c.thread_id == threads.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    return q


def _events_select(channel_ids: list[UUID] | None, community_ids: list[UUID] | None):
    q = select(
        events.c.id,
        literal("event").label("entity_type"),
        events.c.slug,
        events.c.title,
        events.c.description.label("body"),
        events.c.created_by.label("author_id"),
        _ZERO_INT.label("signal_count"),
        events.c.vote_count,
        events.c.comment_count,
        events.c.member_count,
        events.c.going_count,
        events.c.last_activity_at,
        events.c.created_at,
    ).where(events.c.is_private.is_(False))

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(event_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(event_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(event_tags, event_tags.c.event_id == events.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    return q


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


def _serialize_item(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "author_id": row["author_id"],
        "signal_count": int(row["signal_count"] or 0),
        "vote_count": int(row["vote_count"] or 0),
        "comment_count": int(row["comment_count"] or 0),
        "member_count": int(row["member_count"] or 0),
        "going_count": int(row["going_count"] or 0),
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
    }


def _build_feed(
    db: Session,
    sort: str,
    limit: int,
    offset: int,
    channel_ids: list[UUID] | None = None,
    community_ids: list[UUID] | None = None,
) -> dict[str, object]:
    p_q = _projects_select(channel_ids, community_ids)
    t_q = _threads_select(channel_ids, community_ids)
    e_q = _events_select(channel_ids, community_ids)

    parts = [q for q in (p_q, t_q, e_q) if q is not None]

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
    items = [_serialize_item(row) for row in rows]
    return {"total": len(items), "sort": sort, "limit": limit, "offset": offset, "items": items}


def get_public_feed(
    db: Session,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    return _build_feed(db, safe_sort, max(1, min(limit, 100)), max(0, offset))


def get_home_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    channel_ids, community_ids = _get_user_scope_ids(db, current_user_id)
    return _build_feed(
        db, safe_sort, max(1, min(limit, 100)), max(0, offset),
        channel_ids=channel_ids,
        community_ids=community_ids,
    )
