from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_votes,
    event_plans,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_requests,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_votes,
    project_plans,
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
                and_(
                    help_requests.c.ends_at.is_not(None),
                    help_requests.c.ends_at <= now,
                ),
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
                and_(
                    help_requests.c.ends_at.is_not(None),
                    help_requests.c.ends_at <= now,
                ),
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
        key=lambda item: str(item.get("createdAt") or item.get("endsAt") or ""), reverse=True
    )
    _append_cast_votes(db, current_user_id, items)
    _append_current_role_signups(db, current_user_id, items, now)
    items.sort(
        key=lambda item: str(item.get("createdAt") or item.get("endsAt") or ""), reverse=True
    )
    return items[:40]


def _vote_history_item(
    *,
    item_id: str,
    title: str,
    href: str,
    meta: str,
    created_at: object,
    vote: str,
) -> dict[str, object]:
    vote_label = {"yes": "Yes", "no": "No", "neutral": "Neutral"}.get(str(vote), str(vote))
    return {
        "kind": "vote",
        "id": item_id,
        "subjectId": item_id,
        "title": title,
        "href": href,
        "meta": f"You voted {vote_label} · {meta}",
        "createdAt": _small_iso(created_at),
        "viewerParticipated": True,
    }


def _append_cast_votes(db: Session, current_user_id: UUID, items: list[dict[str, object]]) -> None:
    seen = {str(item["id"]) for item in items}
    appended: list[dict[str, object]] = []

    plan_rows = (
        db.execute(
            select(
                project_plan_votes.c.plan_id,
                project_plan_votes.c.vote,
                project_plan_votes.c.created_at,
                project_plans.c.title,
                projects.c.slug,
                projects.c.title.label("parent_title"),
            )
            .select_from(
                project_plan_votes.join(project_plans, project_plans.c.id == project_plan_votes.c.plan_id).join(
                    projects, projects.c.id == project_plans.c.project_id
                )
            )
            .where(project_plan_votes.c.voter_id == current_user_id)
            .order_by(project_plan_votes.c.created_at.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    for row in plan_rows:
        item_id = f"vote-project-plan-{row['plan_id']}"
        if item_id in seen:
            continue
        seen.add(item_id)
        appended.append(
            _vote_history_item(
                item_id=item_id,
                title=row["title"],
                href=f"/projects/{row['slug']}?open=vote&voteKind=plan&voteTarget={row['plan_id']}",
                meta=row["parent_title"],
                created_at=row["created_at"],
                vote=row["vote"],
            )
        )

    phase_rows = (
        db.execute(
            select(
                project_phase_change_votes.c.request_id,
                project_phase_change_votes.c.vote,
                project_phase_change_votes.c.created_at,
                project_phase_change_requests.c.target_phase_id,
                projects.c.slug,
                projects.c.title.label("parent_title"),
            )
            .select_from(
                project_phase_change_votes.join(
                    project_phase_change_requests,
                    project_phase_change_requests.c.id == project_phase_change_votes.c.request_id,
                ).join(projects, projects.c.id == project_phase_change_requests.c.project_id)
            )
            .where(project_phase_change_votes.c.voter_id == current_user_id)
            .order_by(project_phase_change_votes.c.created_at.desc())
            .limit(8)
        )
        .mappings()
        .all()
    )
    for row in phase_rows:
        item_id = f"vote-project-phase-{row['request_id']}"
        if item_id in seen:
            continue
        seen.add(item_id)
        appended.append(
            _vote_history_item(
                item_id=item_id,
                title=f"Phase change · {row['parent_title']}",
                href=f"/projects/{row['slug']}?open=vote&voteKind=phase_change&voteTarget={row['request_id']}",
                meta=f"Advance to {str(row['target_phase_id']).replace('-', ' ').title()}",
                created_at=row["created_at"],
                vote=row["vote"],
            )
        )

    event_plan_rows = (
        db.execute(
            select(
                event_plan_votes.c.plan_id,
                event_plan_votes.c.vote,
                event_plan_votes.c.created_at,
                event_plans.c.title,
                events.c.slug,
                events.c.title.label("parent_title"),
            )
            .select_from(
                event_plan_votes.join(event_plans, event_plans.c.id == event_plan_votes.c.plan_id).join(
                    events, events.c.id == event_plans.c.event_id
                )
            )
            .where(event_plan_votes.c.voter_id == current_user_id)
            .order_by(event_plan_votes.c.created_at.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    for row in event_plan_rows:
        item_id = f"vote-event-plan-{row['plan_id']}"
        if item_id in seen:
            continue
        seen.add(item_id)
        appended.append(
            _vote_history_item(
                item_id=item_id,
                title=row["title"],
                href=f"/events/{row['slug']}?open=vote&voteKind=plan&voteTarget={row['plan_id']}",
                meta=row["parent_title"],
                created_at=row["created_at"],
                vote=row["vote"],
            )
        )

    event_phase_rows = (
        db.execute(
            select(
                event_phase_change_votes.c.request_id,
                event_phase_change_votes.c.vote,
                event_phase_change_votes.c.created_at,
                event_phase_change_requests.c.target_phase_id,
                events.c.slug,
                events.c.title.label("parent_title"),
            )
            .select_from(
                event_phase_change_votes.join(
                    event_phase_change_requests,
                    event_phase_change_requests.c.id == event_phase_change_votes.c.request_id,
                ).join(events, events.c.id == event_phase_change_requests.c.event_id)
            )
            .where(event_phase_change_votes.c.voter_id == current_user_id)
            .order_by(event_phase_change_votes.c.created_at.desc())
            .limit(8)
        )
        .mappings()
        .all()
    )
    for row in event_phase_rows:
        item_id = f"vote-event-phase-{row['request_id']}"
        if item_id in seen:
            continue
        seen.add(item_id)
        appended.append(
            _vote_history_item(
                item_id=item_id,
                title=f"Phase change · {row['parent_title']}",
                href=f"/events/{row['slug']}?open=vote&voteKind=phase_change&voteTarget={row['request_id']}",
                meta=f"Advance to {str(row['target_phase_id']).replace('-', ' ').title()}",
                created_at=row["created_at"],
                vote=row["vote"],
            )
        )

    items.extend(appended)


def _append_current_role_signups(
    db: Session,
    current_user_id: UUID,
    items: list[dict[str, object]],
    now: datetime,
) -> None:
    seen = {str(item["id"]) for item in items}

    project_rows = (
        db.execute(
            select(
                project_activities.c.id,
                project_activities.c.title,
                project_activities.c.scheduled_at,
                projects.c.slug,
                projects.c.title.label("parent_title"),
                projects.c.project_mode,
            )
            .select_from(
                project_activity_assignments.join(
                    project_activity_roles,
                    project_activity_roles.c.id == project_activity_assignments.c.role_id,
                )
                .join(project_activities, project_activities.c.id == project_activity_roles.c.activity_id)
                .join(projects, projects.c.id == project_activities.c.project_id)
            )
            .where(
                project_activity_assignments.c.user_id == current_user_id,
                projects.c.current_phase_id == "phase-5",
                project_activities.c.ends_at > now,
            )
            .order_by(project_activities.c.scheduled_at.desc())
            .limit(10)
        )
        .mappings()
        .all()
    )
    for row in project_rows:
        item_id = f"signup-project-activity-{row['id']}"
        if item_id in seen or str(row["id"]) in seen:
            continue
        seen.add(item_id)
        items.append(
            {
                "kind": "project",
                "id": item_id,
                "subjectId": row["slug"],
                "title": row["title"],
                "href": f"/projects/{row['slug']}?activity={row['id']}",
                "meta": f"You signed up · {row['parent_title']}",
                "createdAt": _small_iso(row["scheduled_at"]),
                "scheduledAt": _small_iso(row["scheduled_at"]),
                "projectMode": row["project_mode"],
                "projectSlug": row["slug"],
                "activityId": str(row["id"]),
                "viewerParticipated": True,
            }
        )

    event_rows = (
        db.execute(
            select(
                event_activities.c.id,
                event_activities.c.title,
                event_activities.c.scheduled_at,
                events.c.slug,
                events.c.title.label("parent_title"),
            )
            .select_from(
                event_activity_assignments.join(
                    event_activity_roles,
                    event_activity_roles.c.id == event_activity_assignments.c.role_id,
                )
                .join(event_activities, event_activities.c.id == event_activity_roles.c.activity_id)
                .join(events, events.c.id == event_activities.c.event_id)
            )
            .where(
                event_activity_assignments.c.user_id == current_user_id,
                events.c.current_phase_id == "activity",
                event_activities.c.ends_at > now,
            )
            .order_by(event_activities.c.scheduled_at.desc())
            .limit(10)
        )
        .mappings()
        .all()
    )
    for row in event_rows:
        item_id = f"signup-event-activity-{row['id']}"
        if item_id in seen or str(row["id"]) in seen:
            continue
        seen.add(item_id)
        items.append(
            {
                "kind": "event",
                "id": item_id,
                "subjectId": row["slug"],
                "title": row["title"],
                "href": f"/events/{row['slug']}?activity={row['id']}",
                "meta": f"You signed up · {row['parent_title']}",
                "createdAt": _small_iso(row["scheduled_at"]),
                "scheduledAt": _small_iso(row["scheduled_at"]),
                "eventSlug": row["slug"],
                "activityId": str(row["id"]),
                "viewerParticipated": True,
            }
        )

    help_rows = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.needed_at,
                help_requests.c.schedule_label,
            )
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                ).join(help_requests, help_requests.c.id == help_request_roles.c.help_request_id)
            )
            .where(help_request_role_assignments.c.user_id == current_user_id)
            .distinct()
            .order_by(help_requests.c.needed_at.desc())
            .limit(10)
        )
        .mappings()
        .all()
    )
    for row in help_rows:
        hr_id = str(row["id"])
        if hr_id in seen:
            continue
        seen.add(hr_id)
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
                "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                "viewerParticipated": True,
            }
        )
