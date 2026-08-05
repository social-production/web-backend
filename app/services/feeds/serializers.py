from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import (
    Integer,
    and_,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    content_votes,
    event_signals,
    event_tags,
    event_updates,
    help_request_tags,
    project_signals,
    project_tags,
    project_updates,
    thread_tags,
)
from app.services.content import _help_request_role_summaries
from app.services.moderation.serialize import load_active_reports_for_targets
from app.services.projects_phases import display_stage_label as project_display_stage_label

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


def _favorability(support_count: int, oppose_count: int) -> float | None:
    total = support_count + oppose_count
    if total <= 0:
        return None
    return support_count / total


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
        project_rows = (
            db.execute(select(ranked_projects).where(ranked_projects.c.rn == 1)).mappings().all()
        )
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
        event_rows = (
            db.execute(select(ranked_events).where(ranked_events.c.rn == 1)).mappings().all()
        )
        for row in event_rows:
            key = str(row["event_id"])
            result[key] = {
                "last_update_at": row["created_at"],
                "latest_update_body": _truncate_update_body(str(row["body"])),
            }

    return result


def _fetch_active_report_keys(
    db: Session,
    rows: list[Mapping[str, object]],
) -> set[str]:
    """Return {"entity_type:id"} keys that currently have an active report."""
    return set(_fetch_active_reports(db, rows).keys())


def _fetch_active_reports(
    db: Session,
    rows: list[Mapping[str, object]],
    current_user_id: UUID | None = None,
) -> dict[str, dict[str, object]]:
    """Return {"entity_type:id"} -> frontend report summary for active reports."""
    ids_by_type: dict[str, list[UUID]] = {}
    for row in rows:
        entity_type = str(row["entity_type"] or "")
        if entity_type == "comment_activity":
            entity_type = "comment"
        if entity_type not in {
            "post",
            "thread",
            "project",
            "event",
            "help_request",
            "comment",
        }:
            continue
        ids_by_type.setdefault(entity_type, []).append(row["id"])

    if not ids_by_type:
        return {}

    reports_by_key: dict[str, dict[str, object]] = {}
    for entity_type, ids in ids_by_type.items():
        loaded = load_active_reports_for_targets(
            db,
            target_type=entity_type,
            target_ids=ids,
            current_user_id=current_user_id,
        )
        for target_id, report in loaded.items():
            feed_type = "comment_activity" if entity_type == "comment" else entity_type
            reports_by_key[f"{feed_type}:{target_id}"] = report
            reports_by_key[f"{entity_type}:{target_id}"] = report
    return reports_by_key


def _moderation_fields(
    row: Mapping[str, object],
    active_report_keys: set[str] | None = None,
    active_reports: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    entity_type = str(row["entity_type"] or "")
    moderation_state = str(row.get("moderation_state") or "visible")
    key = f"{entity_type}:{row['id']}"
    report = (active_reports or {}).get(key)
    has_active_report = (
        moderation_state in {"under_review", "hidden"}
        or report is not None
        or (active_report_keys is not None and key in active_report_keys)
    )
    is_under_review = moderation_state == "under_review" or (
        report is not None and str(report.get("resolution") or "") in {"open", "under_review"}
    )
    return {
        "moderation_state": moderation_state,
        "moderation_reason": row.get("moderation_reason"),
        "is_under_review": is_under_review,
        "has_active_report": has_active_report,
        "report": report,
    }


def _serialize_item(
    row: Mapping[str, object],
    tags: dict[str, dict[str, list[dict[str, str]]]],
    active_votes: dict[str, int] | None = None,
    updates: dict[str, dict[str, object]] | None = None,
    help_request_roles: list[dict[str, object]] | None = None,
    viewer_signals: dict[str, str] | None = None,
    active_report_keys: set[str] | None = None,
    active_reports: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    entity_type = row["entity_type"]
    vote_key = f"{entity_type}:{row['id']}"
    update_data = (updates or {}).get(item_id, {})
    roles_data = help_request_roles if help_request_roles is not None else (row.get("roles") or [])
    signup_count = 0
    slots_needed = 0
    if entity_type == "help_request" and help_request_roles is not None:
        signup_count, slots_needed = _help_request_role_summaries(help_request_roles)
    is_signal_entity = entity_type in ("project", "event")
    support_count = int(row.get("support_count") or 0)
    oppose_count = int(row.get("oppose_count") or 0)
    return {
        "id": item_id,
        "entity_type": entity_type,
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "audience": row["audience"],
        "author_id": row["author_id"],
        "author_username": row["author_username"],
        "author_profile_image_url": row.get("author_profile_image_url"),
        "signal_count": int(row["signal_count"] or 0),
        "support_count": support_count if is_signal_entity else 0,
        "oppose_count": oppose_count if is_signal_entity else 0,
        "favorability": _favorability(support_count, oppose_count) if is_signal_entity else None,
        "viewer_signal": (viewer_signals or {}).get(vote_key) if is_signal_entity else None,
        "vote_count": int(row["vote_count"] or 0),
        **_moderation_fields(row, active_report_keys, active_reports),
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
        "active_vote": 0 if is_signal_entity else int((active_votes or {}).get(vote_key, 0)),
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
    viewer_signals: dict[str, str] | None = None,
    active_report_keys: set[str] | None = None,
    active_reports: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    item_id = str(row["id"])
    tag_data = tags.get(item_id, {"channels": [], "communities": []})
    entity_type = row["entity_type"]
    if entity_type == "comment_activity":
        vote_key = f"comment:{row['id']}"
    else:
        vote_key = f"{entity_type}:{row['id']}"
    update_data = (updates or {}).get(item_id, {})
    is_signal_entity = entity_type in ("project", "event")
    support_count = int(row.get("support_count") or 0)
    oppose_count = int(row.get("oppose_count") or 0)
    return {
        "id": item_id,
        "entity_type": entity_type,
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "audience": row["audience"],
        "author_id": row["author_id"],
        "author_username": row["author_username"],
        "author_profile_image_url": row.get("author_profile_image_url"),
        "signal_count": int(row["signal_count"] or 0),
        "support_count": support_count if is_signal_entity else 0,
        "oppose_count": oppose_count if is_signal_entity else 0,
        "favorability": _favorability(support_count, oppose_count) if is_signal_entity else None,
        "viewer_signal": (viewer_signals or {}).get(vote_key) if is_signal_entity else None,
        "vote_count": int(row["vote_count"] or 0),
        **_moderation_fields(row, active_report_keys, active_reports),
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
        "active_vote": 0 if is_signal_entity else int((active_votes or {}).get(vote_key, 0)),
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
        "help_request": [],
        "comment": [],
    }
    for row in rows:
        entity_type = row["entity_type"]
        if entity_type == "comment_activity":
            item_ids_by_type["comment"].append(row["id"])
            continue
        # Projects and events use signals, not content votes.
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
        select(
            content_votes.c.target_type, content_votes.c.target_id, content_votes.c.direction
        ).where(
            content_votes.c.voter_id == current_user_id,
            or_(*vote_filters),
        )
    ).all()

    return {f"{row[0]}:{row[1]}": int(row[2]) for row in vote_rows}


def _fetch_viewer_signals_for_rows(
    db: Session,
    rows: list[Mapping[str, object]],
    current_user_id: UUID | None,
) -> dict[str, str]:
    """Returns {"project:<id>" | "event:<id>": "demand" | "opposition"} for the viewer's own signals."""
    if current_user_id is None or not rows:
        return {}

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    if not project_ids and not event_ids:
        return {}

    result: dict[str, str] = {}

    if project_ids:
        project_rows = db.execute(
            select(project_signals.c.project_id, project_signals.c.signal_type).where(
                project_signals.c.user_id == current_user_id,
                project_signals.c.project_id.in_(project_ids),
            )
        ).all()
        for project_id, signal_type in project_rows:
            result[f"project:{project_id}"] = signal_type

    if event_ids:
        event_rows = db.execute(
            select(event_signals.c.event_id, event_signals.c.signal_type).where(
                event_signals.c.user_id == current_user_id,
                event_signals.c.event_id.in_(event_ids),
            )
        ).all()
        for event_id, signal_type in event_rows:
            result[f"event:{event_id}"] = signal_type

    return result


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
        rows = (
            db.execute(
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
            )
            .mappings()
            .all()
        )
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
        rows = (
            db.execute(
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
            )
            .mappings()
            .all()
        )
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
        rows = (
            db.execute(
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
            )
            .mappings()
            .all()
        )
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
        rows = (
            db.execute(
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
            )
            .mappings()
            .all()
        )
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
