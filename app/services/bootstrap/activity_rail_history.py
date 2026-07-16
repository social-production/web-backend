from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    event_activities,
    event_memberships,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_requests,
    project_activities,
    project_memberships,
    projects,
)
from app.services.bootstrap.activity_rail import _viewer_assigned_activity_ids
from app.services.bootstrap.summary import _small_iso
from app.services.feeds import _truncate_update_body


def _build_activity_rail_history(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    items: list[dict[str, object]] = []

    proj_history_rows = (
        db.execute(
            select(
                project_activities.c.id,
                project_activities.c.title,
                project_activities.c.scheduled_at,
                project_activities.c.ends_at,
                projects.c.slug.label("parent_slug"),
                projects.c.title.label("parent_title"),
                projects.c.project_mode,
            )
            .select_from(
                project_memberships.join(
                    projects, projects.c.id == project_memberships.c.project_id
                ).join(project_activities, project_activities.c.project_id == projects.c.id)
            )
            .where(
                project_memberships.c.user_id == current_user_id,
                project_activities.c.ends_at <= now,
            )
            .order_by(project_activities.c.ends_at.desc())
            .limit(20)
        )
        .mappings()
        .all()
    )

    evt_history_rows = (
        db.execute(
            select(
                event_activities.c.id,
                event_activities.c.title,
                event_activities.c.scheduled_at,
                event_activities.c.ends_at,
                events.c.slug.label("parent_slug"),
                events.c.title.label("parent_title"),
            )
            .select_from(
                event_memberships.join(events, events.c.id == event_memberships.c.event_id).join(
                    event_activities, event_activities.c.event_id == events.c.id
                )
            )
            .where(
                event_memberships.c.user_id == current_user_id,
                event_activities.c.ends_at <= now,
            )
            .order_by(event_activities.c.ends_at.desc())
            .limit(20)
        )
        .mappings()
        .all()
    )

    project_activity_ids = [row["id"] for row in proj_history_rows]
    event_activity_ids = [row["id"] for row in evt_history_rows]
    assigned_activity_ids = _viewer_assigned_activity_ids(
        db,
        project_activity_ids=project_activity_ids,
        event_activity_ids=event_activity_ids,
        user_id=current_user_id,
    )

    for row in proj_history_rows:
        aid = row["id"]
        items.append(
            {
                "kind": "project",
                "id": str(aid),
                "subjectId": row["parent_slug"],
                "title": row["title"],
                "href": f"/projects/{row['parent_slug']}?activity={aid}",
                "meta": row["parent_title"],
                "createdAt": _small_iso(row["scheduled_at"]),
                "scheduledAt": _small_iso(row["scheduled_at"]),
                "endsAt": _small_iso(row["ends_at"]),
                "projectMode": row["project_mode"],
                "projectSlug": row["parent_slug"],
                "activityId": str(aid),
                "viewerParticipated": aid in assigned_activity_ids,
            }
        )

    for row in evt_history_rows:
        aid = row["id"]
        items.append(
            {
                "kind": "event",
                "id": str(aid),
                "subjectId": row["parent_slug"],
                "title": row["title"],
                "href": f"/events/{row['parent_slug']}?activity={aid}",
                "meta": row["parent_title"],
                "createdAt": _small_iso(row["scheduled_at"]),
                "scheduledAt": _small_iso(row["scheduled_at"]),
                "endsAt": _small_iso(row["ends_at"]),
                "eventSlug": row["parent_slug"],
                "activityId": str(aid),
                "viewerParticipated": aid in assigned_activity_ids,
            }
        )

    past_author_rows = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.body,
                help_requests.c.needed_at,
                help_requests.c.schedule_label,
            )
            .where(
                help_requests.c.author_id == current_user_id,
                help_requests.c.needed_at <= now,
            )
            .order_by(help_requests.c.needed_at.desc())
            .limit(10)
        )
        .mappings()
        .all()
    )

    for row in past_author_rows:
        hr_id = str(row["id"])
        items.append(
            {
                "kind": "help-request-owned",
                "id": hr_id,
                "subjectId": hr_id,
                "title": row["title"],
                "href": f"/help-requests/{hr_id}",
                "meta": "Your request",
                "createdAt": _small_iso(row["needed_at"]),
                "scheduledAt": _small_iso(row["needed_at"]),
                "endsAt": _small_iso(row["needed_at"]),
                "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                "viewerIsAuthor": True,
                "viewerParticipated": True,
                "body": _truncate_update_body(str(row["body"] or "")),
            }
        )

    past_signup_rows = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.body,
                help_requests.c.needed_at,
                help_requests.c.schedule_label,
            )
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                ).join(help_requests, help_requests.c.id == help_request_roles.c.help_request_id)
            )
            .where(
                help_request_role_assignments.c.user_id == current_user_id,
                help_requests.c.needed_at <= now,
            )
            .distinct()
            .order_by(help_requests.c.needed_at.desc())
            .limit(10)
        )
        .mappings()
        .all()
    )

    seen_help_ids = {item["id"] for item in items if item["kind"].startswith("help-request")}
    for row in past_signup_rows:
        hr_id = str(row["id"])
        if hr_id in seen_help_ids:
            continue
        items.append(
            {
                "kind": "help-request-signup",
                "id": hr_id,
                "subjectId": hr_id,
                "title": row["title"],
                "href": f"/help-requests/{hr_id}",
                "meta": "You signed up",
                "createdAt": _small_iso(row["needed_at"]),
                "scheduledAt": _small_iso(row["needed_at"]),
                "endsAt": _small_iso(row["needed_at"]),
                "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                "viewerParticipated": True,
                "body": _truncate_update_body(str(row["body"] or "")),
            }
        )

    items.sort(
        key=lambda item: str(item.get("endsAt") or item.get("createdAt") or ""), reverse=True
    )
    return items[:20]
