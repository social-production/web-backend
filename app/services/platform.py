from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Integer, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    event_tags,
    events,
    platform_board_memberships,
    project_tags,
    projects,
)
from app.services.board import list_board_standing

VALID_SORTS = frozenset({"popular", "recent"})
_ZERO_INT = literal(0, Integer)


def _get_platform_channel(db: Session) -> Mapping[str, object] | None:
    from app.models import channels

    return (
        db.execute(
            select(channels.c.id, channels.c.slug, channels.c.name, channels.c.description)
            .where(channels.c.slug.in_(["platform", "stewardship"]))
            .order_by(channels.c.slug.asc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def _projects_select_for_platform(channel_id: UUID | None):
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

    if channel_id is not None:
        q = q.outerjoin(project_tags, project_tags.c.project_id == projects.c.id).where(
            or_(
                projects.c.is_platform_tagged.is_(True),
                project_tags.c.channel_id == channel_id,
            )
        )
    else:
        q = q.where(projects.c.is_platform_tagged.is_(True))

    return q.distinct()


def _events_select_for_platform(channel_id: UUID | None):
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

    if channel_id is None:
        return None

    return (
        q.join(event_tags, event_tags.c.event_id == events.c.id)
        .where(event_tags.c.channel_id == channel_id)
        .distinct()
    )


def _serialize_feed_item(row: Mapping[str, object]) -> dict[str, object]:
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


def _build_platform_feed(
    db: Session,
    channel_id: UUID | None,
    sort: str,
    limit: int,
    offset: int,
) -> dict[str, object]:
    p_q = _projects_select_for_platform(channel_id)
    e_q = _events_select_for_platform(channel_id)
    parts = [q for q in (p_q, e_q) if q is not None]

    if not parts:
        return {"total": 0, "sort": sort, "limit": limit, "offset": offset, "items": []}

    combined = union_all(*parts).subquery("platform_feed")

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

    rows = (
        db.execute(
            select(combined)
            .order_by(sort_col, combined.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .mappings()
        .all()
    )

    items = [_serialize_feed_item(row) for row in rows]
    return {"total": len(items), "sort": sort, "limit": limit, "offset": offset, "items": items}


def get_platform_page(
    db: Session,
    viewer_user_id: UUID | None,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)

    channel = _get_platform_channel(db)
    channel_id = channel["id"] if channel is not None else None
    board = list_board_standing(db, viewer_user_id=viewer_user_id)

    candidacy_options: dict[str, object] | None = None
    if viewer_user_id is not None:
        membership_row = (
            db.execute(
                select(platform_board_memberships.c.standing_state).where(
                    platform_board_memberships.c.user_id == viewer_user_id
                )
            )
            .mappings()
            .first()
        )
        viewer_state = membership_row["standing_state"] if membership_row is not None else None
        candidacy_options = {
            "viewer_state": viewer_state,
            "can_volunteer": viewer_state is None,
        }

    return {
        "channel": {
            "id": channel["id"],
            "slug": channel["slug"],
            "name": channel["name"],
            "description": channel["description"],
        }
        if channel is not None
        else None,
        "moderators": board["members"],
        "moderator_candidates": board["candidates"],
        "moderator_candidacy_options": candidacy_options,
        "feed": _build_platform_feed(
            db=db,
            channel_id=channel_id,
            sort=safe_sort,
            limit=bounded_limit,
            offset=bounded_offset,
        ),
    }
