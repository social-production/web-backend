from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Integer, and_, cast, func, literal, null, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    content_votes,
    event_tags,
    event_updates,
    events,
    posts,
    project_tags,
    project_updates,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    user_follows,
    users,
)

from app.services.projects_phases import display_stage_label as project_display_stage_label

VALID_SORTS = frozenset({"popular", "recent"})

EVENT_STAGE_LABEL_BY_PHASE_ID = {
    "proposal": "Proposal",
    "event-plan": "Event Plan",
    "activity": "Activity",
    "closed": "Closed",
}

_ZERO_INT = literal(0, Integer)


def _resolved_feed_stage_label(row: Mapping[str, object]) -> str | None:
    entity_type = row["entity_type"]
    if entity_type == "project":
        return project_display_stage_label(
            str(row["project_mode"] or "productive"),
            str(row["project_subtype"]) if row.get("project_subtype") else None,
            str(row.get("current_phase_id") or "phase-1"),
        )
    if entity_type == "event":
        phase_id = str(row.get("current_phase_id") or "proposal")
        return EVENT_STAGE_LABEL_BY_PHASE_ID.get(phase_id, "Proposal")
    stage_label = row.get("stage_label")
    return str(stage_label) if stage_label else None


def _projects_select(channel_ids: list[UUID] | None, community_ids: list[UUID] | None):
    q = select(
        projects.c.id,
        literal("project").label("entity_type"),
        projects.c.slug,
        projects.c.title,
        projects.c.description.label("body"),
        literal(None).label("audience"),
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
        projects.c.current_phase_id,
        projects.c.location_label,
        literal(False, Boolean).label("is_private"),
        cast(null(), DateTime(timezone=True)).label("scheduled_at"),
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
        literal(None).label("audience"),
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
        literal(None).label("current_phase_id"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        cast(null(), DateTime(timezone=True)).label("scheduled_at"),
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
        literal(None).label("audience"),
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
        events.c.current_phase_id,
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


def _truncate_update_body(body: str, limit: int = 200) -> str:
    trimmed = body.strip()
    if len(trimmed) <= limit:
        return trimmed
    return f"{trimmed[:limit].rstrip()}…"


def _fetch_latest_updates_for_items(
    db: Session,
    project_ids: list[UUID],
    event_ids: list[UUID],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}

    if project_ids:
        ranked_projects = (
            select(
                project_updates.c.project_id,
                project_updates.c.body,
                project_updates.c.created_at,
                func.row_number()
                .over(
                    partition_by=project_updates.c.project_id,
                    order_by=project_updates.c.created_at.desc(),
                )
                .label("rn"),
            )
            .where(project_updates.c.project_id.in_(project_ids))
            .subquery()
        )
        project_rows = db.execute(
            select(ranked_projects).where(ranked_projects.c.rn == 1)
        ).mappings().all()
        for row in project_rows:
            key = str(row["project_id"])
            result[key] = {
                "last_update_at": row["created_at"],
                "latest_update_body": _truncate_update_body(str(row["body"])),
            }

    if event_ids:
        ranked_events = (
            select(
                event_updates.c.event_id,
                event_updates.c.body,
                event_updates.c.created_at,
                func.row_number()
                .over(
                    partition_by=event_updates.c.event_id,
                    order_by=event_updates.c.created_at.desc(),
                )
                .label("rn"),
            )
            .where(event_updates.c.event_id.in_(event_ids))
            .subquery()
        )
        event_rows = db.execute(
            select(ranked_events).where(ranked_events.c.rn == 1)
        ).mappings().all()
        for row in event_rows:
            key = str(row["event_id"])
            result[key] = {
                "last_update_at": row["created_at"],
                "latest_update_body": _truncate_update_body(str(row["body"])),
            }

    return result


def _serialize_item(row: Mapping[str, object], tags: dict[str, dict[str, list[dict[str, str]]]], active_votes: dict[str, int] | None = None, updates: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    vote_key = f"{row['entity_type']}:{row['id']}"
    update_data = (updates or {}).get(item_id, {})
    return {
        "id": item_id,
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "audience": row["audience"],
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
        "stage_label": _resolved_feed_stage_label(row),
        "current_phase_id": row.get("current_phase_id"),
        "location_label": row["location_label"],
        "is_private": bool(row["is_private"]),
        "scheduled_at": row["scheduled_at"],
        "time_label": row["time_label"],
        "active_vote": int((active_votes or {}).get(vote_key, 0)),
        "channel_tags": tag_data["channels"],
        "community_tags": tag_data["communities"],
        "last_update_at": update_data.get("last_update_at"),
        "latest_update_body": update_data.get("latest_update_body"),
    }


def _serialize_personal_item(
    row: Mapping[str, object],
    tags: dict[str, dict[str, list[dict[str, str]]]],
    active_votes: dict[str, int] | None = None,
    updates: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    vote_key = f"{row['entity_type']}:{row['id']}"
    update_data = (updates or {}).get(item_id, {})
    return {
        "id": item_id,
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "audience": row["audience"],
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
        "stage_label": _resolved_feed_stage_label(row),
        "current_phase_id": row.get("current_phase_id"),
        "location_label": row["location_label"],
        "is_private": bool(row["is_private"]),
        "scheduled_at": row["scheduled_at"],
        "time_label": row["time_label"],
        "active_vote": int((active_votes or {}).get(vote_key, 0)),
        "channel_tags": tag_data["channels"],
        "community_tags": tag_data["communities"],
        "last_update_at": update_data.get("last_update_at"),
        "latest_update_body": update_data.get("latest_update_body"),
    }


def _fetch_active_votes_for_rows(
    db: Session,
    rows: list[Mapping[str, object]],
    current_user_id: UUID | None,
) -> dict[str, int]:
    if current_user_id is None or not rows:
        return {}

    item_ids_by_type: dict[str, list[UUID]] = {"post": [], "thread": [], "project": [], "event": []}
    for row in rows:
        entity_type = row["entity_type"]
        if entity_type in item_ids_by_type:
            item_ids_by_type[entity_type].append(row["id"])

    vote_filters = [
        and_(content_votes.c.target_type == entity_type, content_votes.c.target_id.in_(item_ids))
        for entity_type, item_ids in item_ids_by_type.items()
        if item_ids
    ]
    if not vote_filters:
        return {}

    vote_rows = db.execute(
        select(content_votes.c.target_type, content_votes.c.target_id, content_votes.c.direction).where(
            content_votes.c.voter_id == current_user_id,
            or_(*vote_filters),
        )
    ).all()

    return {f"{row[0]}:{row[1]}": int(row[2]) for row in vote_rows}


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
        posts.c.audience,
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
        literal(None).label("current_phase_id"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        cast(null(), DateTime(timezone=True)).label("scheduled_at"),
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
        literal(None).label("audience"),
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
        projects.c.current_phase_id,
        projects.c.location_label,
        literal(False, Boolean).label("is_private"),
        cast(null(), DateTime(timezone=True)).label("scheduled_at"),
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
        literal(None).label("audience"),
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
        literal(None).label("current_phase_id"),
        literal(None).label("location_label"),
        literal(False, Boolean).label("is_private"),
        cast(null(), DateTime(timezone=True)).label("scheduled_at"),
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
        literal(None).label("audience"),
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
        events.c.current_phase_id,
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
    current_user_id: UUID | None = None,
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
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    items = [_serialize_item(row, tags, active_votes, updates) for row in rows]
    return {"total": len(items), "sort": sort, "limit": limit, "offset": offset, "items": items}


def get_public_feed(
    db: Session,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    return _build_feed(db, safe_sort, max(1, min(limit, 100)), max(0, offset), current_user_id=current_user_id)


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
        current_user_id=current_user_id,
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
    # Always include the viewer's own posts alongside posts from followed users.
    post_author_ids = list({current_user_id, *followed_user_ids})

    parts = [
        _posts_select_for_followed(post_author_ids),
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
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    items = [_serialize_personal_item(row, tags, active_votes, updates) for row in rows]
    return {
        "total": len(items),
        "sort": safe_sort,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_user_feed(
    db: Session,
    username: str,
    viewer_user_id: UUID | None = None,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)

    user_row = db.execute(
        select(users.c.id).where(users.c.username == username.strip().lower())
    ).first()
    if user_row is None:
        return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}

    user_id: UUID = user_row[0]
    viewer_is_owner = viewer_user_id is not None and viewer_user_id == user_id

    posts_q = (
        select(
            posts.c.id,
            literal("post").label("entity_type"),
            literal(None).label("slug"),
            literal("Post").label("title"),
            posts.c.body,
            posts.c.audience,
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
            literal(None).label("current_phase_id"),
            literal(None).label("location_label"),
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(
            posts.c.author_id == user_id,
            posts.c.audience.in_(["public", "followers"]) if viewer_is_owner else posts.c.audience == "public",
        )
    )

    threads_q = (
        select(
            threads.c.id,
            literal("thread").label("entity_type"),
            threads.c.slug,
            threads.c.title,
            threads.c.body,
            literal(None).label("audience"),
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
            literal(None).label("current_phase_id"),
            literal(None).label("location_label"),
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(threads.c.author_id == user_id)
    )

    events_q = (
        select(
            events.c.id,
            literal("event").label("entity_type"),
            events.c.slug,
            events.c.title,
            events.c.description.label("body"),
            literal(None).label("audience"),
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
            events.c.current_phase_id,
            events.c.location_label,
            events.c.is_private,
            events.c.scheduled_at,
            events.c.time_label,
        )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(events.c.created_by == user_id)
    )

    projects_q = (
        select(
            projects.c.id,
            literal("project").label("entity_type"),
            projects.c.slug,
            projects.c.title,
            projects.c.description.label("body"),
            literal(None).label("audience"),
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
            projects.c.current_phase_id,
            projects.c.location_label,
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
        )
        .select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))
        .where(projects.c.author_id == user_id, projects.c.is_closed.is_(False))
    )

    combined = union_all(posts_q, threads_q, events_q, projects_q).subquery("user_feed")

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
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, viewer_user_id)
    items = [_serialize_personal_item(row, tags, active_votes, updates) for row in rows]
    return {
        "total": len(items),
        "sort": safe_sort,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_scope_feed(
    db: Session,
    scope_kind: str,
    slug: str,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    normalized_slug = slug.strip().lower()

    if scope_kind == "channel":
        row = db.execute(
            select(channels.c.id).where(channels.c.slug == normalized_slug)
        ).first()
        if row is None:
            return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
        return _build_feed(db, safe_sort, bounded_limit, bounded_offset, channel_ids=[row[0]], community_ids=[], current_user_id=current_user_id)

    if scope_kind == "community":
        row = db.execute(
            select(communities.c.id).where(communities.c.slug == normalized_slug)
        ).first()
        if row is None:
            return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
        return _build_feed(db, safe_sort, bounded_limit, bounded_offset, channel_ids=[], community_ids=[row[0]], current_user_id=current_user_id)

    return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
