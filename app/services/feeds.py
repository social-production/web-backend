from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Boolean, Integer, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    event_tags,
    events,
    posts,
    project_tags,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    user_follows,
    users,
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
        users.c.username.label("author_username"),
        projects.c.signal_count,
        projects.c.vote_count,
        projects.c.comment_count,
        projects.c.member_count,
        _ZERO_INT.label("going_count"),
        projects.c.last_activity_at,
        projects.c.created_at,
        projects.c.project_mode,
        projects.c.project_subtype,
        projects.c.stage_label,
        projects.c.location_label,
        literal(False, Boolean).label("is_private"),
        literal(None).label("scheduled_at"),
        literal(None).label("time_label"),
    ).where(projects.c.is_closed.is_(False))
    q = q.select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))

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
        users.c.username.label("author_username"),
        _ZERO_INT.label("signal_count"),
        threads.c.vote_count,
        threads.c.comment_count,
        _ZERO_INT.label("member_count"),
        _ZERO_INT.label("going_count"),
        threads.c.last_activity_at,
        threads.c.created_at,
        literal(None).label("project_mode"),
        literal(None).label("project_subtype"),
        literal(None).label("stage_label"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        literal(None).label("scheduled_at"),
        literal(None).label("time_label"),
    )
    q = q.select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))

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
        users.c.username.label("author_username"),
        _ZERO_INT.label("signal_count"),
        events.c.vote_count,
        events.c.comment_count,
        events.c.member_count,
        events.c.going_count,
        events.c.last_activity_at,
        events.c.created_at,
        literal(None).label("project_mode"),
        literal(None).label("project_subtype"),
        literal(None).label("stage_label"),
        events.c.location_label,
        events.c.is_private,
        events.c.scheduled_at,
        events.c.time_label,
    ).where(events.c.is_private.is_(False))
    q = q.select_from(events.outerjoin(users, users.c.id == events.c.created_by))

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


def _serialize_item(row: Mapping[str, object], tags: dict[str, dict[str, list[dict[str, str]]]]) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    return {
        "id": item_id,
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "author_id": row["author_id"],
        "author_username": row["author_username"],
        "signal_count": int(row["signal_count"] or 0),
        "vote_count": int(row["vote_count"] or 0),
        "comment_count": int(row["comment_count"] or 0),
        "member_count": int(row["member_count"] or 0),
        "going_count": int(row["going_count"] or 0),
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "project_mode": row["project_mode"],
        "project_subtype": row["project_subtype"],
        "stage_label": row["stage_label"],
        "location_label": row["location_label"],
        "is_private": bool(row["is_private"]),
        "scheduled_at": row["scheduled_at"],
        "time_label": row["time_label"],
        "channel_tags": tag_data["channels"],
        "community_tags": tag_data["communities"],
    }


def _serialize_personal_item(
    row: Mapping[str, object],
    tags: dict[str, dict[str, list[dict[str, str]]]],
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    return {
        "id": item_id,
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "author_id": row["author_id"],
        "author_username": row["author_username"],
        "signal_count": int(row["signal_count"] or 0),
        "vote_count": int(row["vote_count"] or 0),
        "comment_count": int(row["comment_count"] or 0),
        "member_count": int(row["member_count"] or 0),
        "going_count": int(row["going_count"] or 0),
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "project_mode": row["project_mode"],
        "project_subtype": row["project_subtype"],
        "stage_label": row["stage_label"],
        "location_label": row["location_label"],
        "is_private": bool(row["is_private"]),
        "scheduled_at": row["scheduled_at"],
        "time_label": row["time_label"],
        "channel_tags": tag_data["channels"],
        "community_tags": tag_data["communities"],
    }


def _get_followed_user_ids(db: Session, current_user_id: UUID) -> list[UUID]:
    rows = db.execute(
        select(user_follows.c.followed_id)
        .where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
    ).all()
    return [row[0] for row in rows]


def _posts_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
        select(
        posts.c.id,
        literal("post").label("entity_type"),
        literal(None).label("slug"),
        literal("Post").label("title"),
        posts.c.body,
        posts.c.author_id,
        users.c.username.label("author_username"),
        _ZERO_INT.label("signal_count"),
        posts.c.vote_count,
        posts.c.comment_count,
        _ZERO_INT.label("member_count"),
        _ZERO_INT.label("going_count"),
        posts.c.updated_at.label("last_activity_at"),
        posts.c.created_at,
        literal(None).label("project_mode"),
        literal(None).label("project_subtype"),
        literal(None).label("stage_label"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        literal(None).label("scheduled_at"),
        literal(None).label("time_label"),
    )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(posts.c.author_id.in_(followed_user_ids))
    )


def _projects_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
        select(
        projects.c.id,
        literal("project").label("entity_type"),
        projects.c.slug,
        projects.c.title,
        projects.c.description.label("body"),
        projects.c.author_id,
        users.c.username.label("author_username"),
        projects.c.signal_count,
        projects.c.vote_count,
        projects.c.comment_count,
        projects.c.member_count,
        _ZERO_INT.label("going_count"),
        projects.c.last_activity_at,
        projects.c.created_at,
        projects.c.project_mode,
        projects.c.project_subtype,
        projects.c.stage_label,
        projects.c.location_label,
        literal(False, Boolean).label("is_private"),
        literal(None).label("scheduled_at"),
        literal(None).label("time_label"),
    )
        .select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))
        .where(
            projects.c.author_id.in_(followed_user_ids),
            projects.c.is_closed.is_(False),
        )
    )


def _threads_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
        select(
        threads.c.id,
        literal("thread").label("entity_type"),
        threads.c.slug,
        threads.c.title,
        threads.c.body,
        threads.c.author_id,
        users.c.username.label("author_username"),
        _ZERO_INT.label("signal_count"),
        threads.c.vote_count,
        threads.c.comment_count,
        _ZERO_INT.label("member_count"),
        _ZERO_INT.label("going_count"),
        threads.c.last_activity_at,
        threads.c.created_at,
        literal(None).label("project_mode"),
        literal(None).label("project_subtype"),
        literal(None).label("stage_label"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        literal(None).label("scheduled_at"),
        literal(None).label("time_label"),
    )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(threads.c.author_id.in_(followed_user_ids))
    )


def _events_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
        select(
        events.c.id,
        literal("event").label("entity_type"),
        events.c.slug,
        events.c.title,
        events.c.description.label("body"),
        events.c.created_by.label("author_id"),
        users.c.username.label("author_username"),
        _ZERO_INT.label("signal_count"),
        events.c.vote_count,
        events.c.comment_count,
        events.c.member_count,
        events.c.going_count,
        events.c.last_activity_at,
        events.c.created_at,
        literal(None).label("project_mode"),
        literal(None).label("project_subtype"),
        literal(None).label("stage_label"),
        events.c.location_label,
        events.c.is_private,
        events.c.scheduled_at,
        events.c.time_label,
    )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(
            events.c.created_by.in_(followed_user_ids),
            events.c.is_private.is_(False),
        )
    )


def _fetch_tags_for_items(
    db: Session,
    project_ids: list[UUID],
    thread_ids: list[UUID],
    event_ids: list[UUID],
) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Returns {entity_id_str: {'channels': [...], 'communities': [...]}}."""
    result: dict[str, dict[str, list[dict[str, str]]]] = {}

    if project_ids:
        rows = db.execute(
            select(
                project_tags.c.project_id.label("entity_id"),
                project_tags.c.tag_kind,
                channels.c.slug.label("channel_slug"),
                channels.c.name.label("channel_name"),
                communities.c.slug.label("community_slug"),
                communities.c.name.label("community_name"),
            )
            .select_from(project_tags)
            .outerjoin(channels, channels.c.id == project_tags.c.channel_id)
            .outerjoin(communities, communities.c.id == project_tags.c.community_id)
            .where(project_tags.c.project_id.in_(project_ids))
        ).mappings().all()
        for row in rows:
            key = str(row["entity_id"])
            bucket = result.setdefault(key, {"channels": [], "communities": []})
            if row["channel_slug"]:
                bucket["channels"].append(
                    {"slug": row["channel_slug"], "label": row["channel_name"], "kind": "channel"}
                )
            if row["community_slug"]:
                bucket["communities"].append(
                    {
                        "slug": row["community_slug"],
                        "label": row["community_name"],
                        "kind": "community",
                    }
                )

    if thread_ids:
        rows = db.execute(
            select(
                thread_tags.c.thread_id.label("entity_id"),
                thread_tags.c.tag_kind,
                channels.c.slug.label("channel_slug"),
                channels.c.name.label("channel_name"),
                communities.c.slug.label("community_slug"),
                communities.c.name.label("community_name"),
            )
            .select_from(thread_tags)
            .outerjoin(channels, channels.c.id == thread_tags.c.channel_id)
            .outerjoin(communities, communities.c.id == thread_tags.c.community_id)
            .where(thread_tags.c.thread_id.in_(thread_ids))
        ).mappings().all()
        for row in rows:
            key = str(row["entity_id"])
            bucket = result.setdefault(key, {"channels": [], "communities": []})
            if row["channel_slug"]:
                bucket["channels"].append(
                    {"slug": row["channel_slug"], "label": row["channel_name"], "kind": "channel"}
                )
            if row["community_slug"]:
                bucket["communities"].append(
                    {
                        "slug": row["community_slug"],
                        "label": row["community_name"],
                        "kind": "community",
                    }
                )

    if event_ids:
        rows = db.execute(
            select(
                event_tags.c.event_id.label("entity_id"),
                event_tags.c.tag_kind,
                channels.c.slug.label("channel_slug"),
                channels.c.name.label("channel_name"),
                communities.c.slug.label("community_slug"),
                communities.c.name.label("community_name"),
            )
            .select_from(event_tags)
            .outerjoin(channels, channels.c.id == event_tags.c.channel_id)
            .outerjoin(communities, communities.c.id == event_tags.c.community_id)
            .where(event_tags.c.event_id.in_(event_ids))
        ).mappings().all()
        for row in rows:
            key = str(row["entity_id"])
            bucket = result.setdefault(key, {"channels": [], "communities": []})
            if row["channel_slug"]:
                bucket["channels"].append(
                    {"slug": row["channel_slug"], "label": row["channel_name"], "kind": "channel"}
                )
            if row["community_slug"]:
                bucket["communities"].append(
                    {
                        "slug": row["community_slug"],
                        "label": row["community_name"],
                        "kind": "community",
                    }
                )

    return result


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
    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids)
    items = [_serialize_item(row, tags) for row in rows]
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


def get_personal_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)

    followed_user_ids = _get_followed_user_ids(db, current_user_id)
    if not followed_user_ids:
        return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}

    parts = [
        _posts_select_for_followed(followed_user_ids),
        _projects_select_for_followed(followed_user_ids),
        _threads_select_for_followed(followed_user_ids),
        _events_select_for_followed(followed_user_ids),
    ]
    parts = [part for part in parts if part is not None]

    if not parts:
        return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}

    combined = union_all(*parts).subquery("personal_feed")

    if safe_sort == "popular":
        sort_col = (
            combined.c.signal_count
            + combined.c.vote_count
            + combined.c.comment_count
            + combined.c.member_count
            + combined.c.going_count
        ).desc()
    else:
        sort_col = combined.c.last_activity_at.desc()

    rows = db.execute(
        select(combined)
        .order_by(sort_col, combined.c.created_at.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    ).mappings().all()

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids)
    items = [_serialize_personal_item(row, tags) for row in rows]
    return {
        "total": len(items),
        "sort": safe_sort,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }
