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


def _serialize_item(
    row: Mapping[str, object],
    tags: dict[str, dict[str, list[dict[str, str]]]],
    active_votes: dict[str, int] | None = None,
    updates: dict[str, dict[str, object]] | None = None,
    help_request_roles: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    vote_key = f"{row['entity_type']}:{row['id']}"
    update_data = (updates or {}).get(item_id, {})
    roles_data = help_request_roles if help_request_roles is not None else (row.get("roles") or [])
    signup_count = 0
    slots_needed = 0
    if row["entity_type"] == "help_request" and help_request_roles is not None:
        signup_count, slots_needed = _help_request_role_summaries(help_request_roles)
    return {
        "id": item_id,
        "entity_type": row["entity_type"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "audience": row["audience"],
        "author_id": row["author_id"],
        "author_username": row["author_username"],
        "author_profile_image_url": row.get("author_profile_image_url"),
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
        "roles": roles_data,
        "signup_count": signup_count,
        "slots_needed": slots_needed,
    }


def _serialize_personal_item(
    row: Mapping[str, object],
    tags: dict[str, dict[str, list[dict[str, str]]]],
    active_votes: dict[str, int] | None = None,
    updates: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    if row["entity_type"] == "comment_activity":
        vote_key = f"comment:{row['id']}"
    else:
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
        "author_profile_image_url": row.get("author_profile_image_url"),
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
        "feed_source": row.get("feed_source", "following"),
        "roles": row.get("roles") or [],
    }


def _fetch_active_votes_for_rows(
    db: Session,
    rows: list[Mapping[str, object]],
    current_user_id: UUID | None,
) -> dict[str, int]:
    if current_user_id is None or not rows:
        return {}

    item_ids_by_type: dict[str, list[UUID]] = {
        "post": [],
        "thread": [],
        "project": [],
        "event": [],
        "help_request": [],
        "comment": [],
    }
    for row in rows:
        entity_type = row["entity_type"]
        if entity_type == "comment_activity":
            item_ids_by_type["comment"].append(row["id"])
            continue
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


def _fetch_tags_for_items(
    db: Session,
    project_ids: list[UUID],
    thread_ids: list[UUID],
    event_ids: list[UUID],
    help_request_ids: list[UUID] | None = None,
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

    if help_request_ids:
        rows = db.execute(
            select(
                help_request_tags.c.help_request_id.label("entity_id"),
                help_request_tags.c.tag_kind,
                channels.c.slug.label("channel_slug"),
                channels.c.name.label("channel_name"),
                communities.c.slug.label("community_slug"),
                communities.c.name.label("community_name"),
            )
            .select_from(help_request_tags)
            .outerjoin(channels, channels.c.id == help_request_tags.c.channel_id)
            .outerjoin(communities, communities.c.id == help_request_tags.c.community_id)
            .where(help_request_tags.c.help_request_id.in_(help_request_ids))
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
