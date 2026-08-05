"""Effective location labels for feed cards based on plans and scheduled activities."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import event_activities, event_plans, project_activities

PLACEHOLDER_LABELS = frozenset(
    {
        "",
        "not specified",
        "tbd",
        "to be decided",
    }
)


def is_placeholder_label(label: str | None) -> bool:
    return (label or "").strip().lower() in PLACEHOLDER_LABELS


def _load_project_activity_locations(db: Session, project_ids: list[UUID]) -> dict[str, str]:
    if not project_ids:
        return {}
    now = datetime.now(UTC)
    rows = (
        db.execute(
            select(
                project_activities.c.project_id,
                project_activities.c.location_label,
                project_activities.c.scheduled_at,
            )
            .where(
                project_activities.c.project_id.in_(project_ids),
                project_activities.c.scheduled_at.is_not(None),
                project_activities.c.scheduled_at >= now,
            )
            .order_by(project_activities.c.scheduled_at.asc())
        )
        .mappings()
        .all()
    )
    result: dict[str, str] = {}
    for row in rows:
        key = str(row["project_id"])
        if key in result:
            continue
        label = str(row["location_label"] or "").strip()
        if label and not is_placeholder_label(label):
            result[key] = label
    return result


def _load_event_display_locations(db: Session, event_ids: list[UUID]) -> dict[str, str]:
    if not event_ids:
        return {}

    now = datetime.now(UTC)
    activity_rows = (
        db.execute(
            select(
                event_activities.c.event_id,
                event_activities.c.location_label,
                event_activities.c.scheduled_at,
            )
            .where(
                event_activities.c.event_id.in_(event_ids),
                event_activities.c.scheduled_at.is_not(None),
                event_activities.c.scheduled_at >= now,
            )
            .order_by(event_activities.c.scheduled_at.asc())
        )
        .mappings()
        .all()
    )
    result: dict[str, str] = {}
    for row in activity_rows:
        key = str(row["event_id"])
        if key in result:
            continue
        label = str(row["location_label"] or "").strip()
        if label and not is_placeholder_label(label):
            result[key] = label

    missing = [event_id for event_id in event_ids if str(event_id) not in result]
    if not missing:
        return result

    plan_rows = (
        db.execute(
            select(event_plans.c.event_id, event_plans.c.location_label).where(
                event_plans.c.event_id.in_(missing),
                event_plans.c.is_leading.is_(True),
            )
        )
        .mappings()
        .all()
    )
    for row in plan_rows:
        key = str(row["event_id"])
        if key in result:
            continue
        label = str(row["location_label"] or "").strip()
        if label and not is_placeholder_label(label):
            result[key] = label
    return result


def apply_feed_location_labels(
    db: Session, items: list[dict[str, object]]
) -> list[dict[str, object]]:
    project_ids = [UUID(str(item["id"])) for item in items if item.get("entity_type") == "project"]
    event_ids = [UUID(str(item["id"])) for item in items if item.get("entity_type") == "event"]

    project_locations = _load_project_activity_locations(db, project_ids)
    event_locations = _load_event_display_locations(db, event_ids)

    for item in items:
        entity_type = str(item.get("entity_type") or "")
        item_id = str(item.get("id") or "")

        if entity_type == "project":
            label = project_locations.get(item_id)
            item["location_label"] = label
            continue

        if entity_type == "event":
            label = event_locations.get(item_id)
            if label:
                item["location_label"] = label
            elif is_placeholder_label(str(item.get("location_label") or "")):
                item["location_label"] = None
            continue

        if entity_type == "help_request":
            if is_placeholder_label(str(item.get("location_label") or "")):
                item["location_label"] = None

    return items
