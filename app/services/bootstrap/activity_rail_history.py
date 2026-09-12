from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    detail_link_request_votes,
    detail_link_requests,
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_edit_request_votes,
    event_edit_requests,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_votes,
    event_plans,
    event_update_request_votes,
    event_update_requests,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_requests,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_merge_capability_change_requests,
    project_merge_capability_change_votes,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_votes,
    project_plans,
    project_pull_request_votes,
    project_pull_requests,
    project_repository_replacement_requests,
    project_repository_replacement_votes,
    project_update_request_votes,
    project_update_requests,
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


def _phase_label(phase_id: object) -> str:
    return str(phase_id).replace("-", " ").title()


def _vote_href(surface: str, slug: object, vote_kind: str, target_id: object) -> str:
    return f"/{surface}/{slug}?open=vote&voteKind={vote_kind}&voteTarget={target_id}"


def _remember_vote(
    seen: set[str],
    appended: list[dict[str, object]],
    *,
    item_id: str,
    title: str,
    href: str,
    meta: str,
    created_at: object,
    vote: str,
) -> None:
    if item_id in seen:
        return
    seen.add(item_id)
    appended.append(
        _vote_history_item(
            item_id=item_id,
            title=title,
            href=href,
            meta=meta,
            created_at=created_at,
            vote=vote,
        )
    )


def _append_request_votes(
    db: Session,
    current_user_id: UUID,
    *,
    vote_table,
    request_table,
    parent_table,
    parent_fk,
    item_id_prefix: str,
    vote_kind: str,
    surface: str,
    seen: set[str],
    appended: list[dict[str, object]],
    title_column=None,
    title_prefix: str | None = None,
    truncate_title: bool = False,
    limit: int = 12,
) -> None:
    title_expr = title_column if title_column is not None else parent_table.c.title
    rows = (
        db.execute(
            select(
                vote_table.c.request_id,
                vote_table.c.vote,
                vote_table.c.created_at,
                parent_table.c.slug,
                parent_table.c.title.label("parent_title"),
                title_expr.label("title"),
            )
            .select_from(
                vote_table.join(request_table, request_table.c.id == vote_table.c.request_id).join(
                    parent_table, parent_fk == parent_table.c.id
                )
            )
            .where(vote_table.c.voter_id == current_user_id)
            .order_by(vote_table.c.created_at.desc())
            .limit(limit)
        )
        .mappings()
        .all()
    )
    for row in rows:
        title = (
            str(row["title"])
            if title_column is not None
            else f"{title_prefix} · {row['parent_title']}"
        )
        if truncate_title:
            title = _truncate_update_body(title, 80)
        meta = row["parent_title"] if title_column is not None else (title_prefix or vote_kind)
        _remember_vote(
            seen,
            appended,
            item_id=f"{item_id_prefix}{row['request_id']}",
            title=title,
            href=_vote_href(surface, row["slug"], vote_kind, row["request_id"]),
            meta=str(meta),
            created_at=row["created_at"],
            vote=row["vote"],
        )


def _append_link_votes(
    db: Session,
    current_user_id: UUID,
    seen: set[str],
    appended: list[dict[str, object]],
) -> None:
    rows = (
        db.execute(
            select(
                detail_link_request_votes.c.request_id,
                detail_link_request_votes.c.vote,
                detail_link_request_votes.c.vote_scope,
                detail_link_request_votes.c.created_at,
                detail_link_requests.c.request_type,
                detail_link_requests.c.source_kind,
                detail_link_requests.c.source_project_id,
                detail_link_requests.c.source_event_id,
                detail_link_requests.c.target_kind,
                detail_link_requests.c.target_project_id,
                detail_link_requests.c.target_event_id,
            )
            .select_from(
                detail_link_request_votes.join(
                    detail_link_requests,
                    detail_link_requests.c.id == detail_link_request_votes.c.request_id,
                )
            )
            .where(detail_link_request_votes.c.voter_id == current_user_id)
            .order_by(detail_link_request_votes.c.created_at.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    for row in rows:
        vote_scope = str(row["vote_scope"] or "source")
        if vote_scope == "source":
            kind = str(row["source_kind"])
            entity_id = row["source_project_id"] if kind == "project" else row["source_event_id"]
        else:
            kind = str(row["target_kind"])
            entity_id = row["target_project_id"] if kind == "project" else row["target_event_id"]
        if entity_id is None:
            continue
        if kind == "project":
            subject = (
                db.execute(
                    select(projects.c.slug, projects.c.title).where(projects.c.id == entity_id)
                )
                .mappings()
                .first()
            )
            surface = "projects"
        else:
            subject = (
                db.execute(select(events.c.slug, events.c.title).where(events.c.id == entity_id))
                .mappings()
                .first()
            )
            surface = "events"
        if subject is None:
            continue
        vote_kind = "link_sever" if str(row["request_type"] or "create") == "sever" else "link"
        _remember_vote(
            seen,
            appended,
            item_id=f"vote-{vote_kind}-{row['request_id']}-{vote_scope}",
            title=subject["title"],
            href=f"/{surface}/{subject['slug']}?tab=links&linkRequest={row['request_id']}",
            meta="Sever link" if vote_kind == "link_sever" else "Link vote",
            created_at=row["created_at"],
            vote=row["vote"],
        )


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
                project_plan_votes.join(
                    project_plans, project_plans.c.id == project_plan_votes.c.plan_id
                ).join(projects, projects.c.id == project_plans.c.project_id)
            )
            .where(project_plan_votes.c.voter_id == current_user_id)
            .order_by(project_plan_votes.c.created_at.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    for row in plan_rows:
        _remember_vote(
            seen,
            appended,
            item_id=f"vote-project-plan-{row['plan_id']}",
            title=row["title"],
            href=_vote_href("projects", row["slug"], "plan", row["plan_id"]),
            meta=row["parent_title"],
            created_at=row["created_at"],
            vote=row["vote"],
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
        _remember_vote(
            seen,
            appended,
            item_id=f"vote-project-phase-{row['request_id']}",
            title=f"Phase change · {row['parent_title']}",
            href=_vote_href("projects", row["slug"], "phase_change", row["request_id"]),
            meta=f"Advance to {_phase_label(row['target_phase_id'])}",
            created_at=row["created_at"],
            vote=row["vote"],
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
                event_plan_votes.join(
                    event_plans, event_plans.c.id == event_plan_votes.c.plan_id
                ).join(events, events.c.id == event_plans.c.event_id)
            )
            .where(event_plan_votes.c.voter_id == current_user_id)
            .order_by(event_plan_votes.c.created_at.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    for row in event_plan_rows:
        _remember_vote(
            seen,
            appended,
            item_id=f"vote-event-plan-{row['plan_id']}",
            title=row["title"],
            href=_vote_href("events", row["slug"], "plan", row["plan_id"]),
            meta=row["parent_title"],
            created_at=row["created_at"],
            vote=row["vote"],
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
        _remember_vote(
            seen,
            appended,
            item_id=f"vote-event-phase-{row['request_id']}",
            title=f"Phase change · {row['parent_title']}",
            href=_vote_href("events", row["slug"], "phase_change", row["request_id"]),
            meta=f"Advance to {_phase_label(row['target_phase_id'])}",
            created_at=row["created_at"],
            vote=row["vote"],
        )

    _append_request_votes(
        db,
        current_user_id,
        vote_table=project_update_request_votes,
        request_table=project_update_requests,
        parent_table=projects,
        parent_fk=project_update_requests.c.project_id,
        item_id_prefix="vote-project-update-",
        vote_kind="update",
        surface="projects",
        seen=seen,
        appended=appended,
        title_column=project_update_requests.c.body,
        truncate_title=True,
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=event_update_request_votes,
        request_table=event_update_requests,
        parent_table=events,
        parent_fk=event_update_requests.c.event_id,
        item_id_prefix="vote-event-update-",
        vote_kind="update",
        surface="events",
        seen=seen,
        appended=appended,
        title_column=event_update_requests.c.body,
        truncate_title=True,
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=project_edit_request_votes,
        request_table=project_edit_requests,
        parent_table=projects,
        parent_fk=project_edit_requests.c.project_id,
        item_id_prefix="vote-project-edit-",
        vote_kind="edit",
        surface="projects",
        seen=seen,
        appended=appended,
        title_column=project_edit_requests.c.title,
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=event_edit_request_votes,
        request_table=event_edit_requests,
        parent_table=events,
        parent_fk=event_edit_requests.c.event_id,
        item_id_prefix="vote-event-edit-",
        vote_kind="edit",
        surface="events",
        seen=seen,
        appended=appended,
        title_column=event_edit_requests.c.title,
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=project_pull_request_votes,
        request_table=project_pull_requests,
        parent_table=projects,
        parent_fk=project_pull_requests.c.project_id,
        item_id_prefix="vote-project-pr-",
        vote_kind="pull_request",
        surface="projects",
        seen=seen,
        appended=appended,
        title_column=project_pull_requests.c.title,
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=project_merge_capability_change_votes,
        request_table=project_merge_capability_change_requests,
        parent_table=projects,
        parent_fk=project_merge_capability_change_requests.c.project_id,
        item_id_prefix="vote-project-merge-capability-",
        vote_kind="merge_capability",
        surface="projects",
        seen=seen,
        appended=appended,
        title_prefix="Merge capability",
    )
    _append_request_votes(
        db,
        current_user_id,
        vote_table=project_repository_replacement_votes,
        request_table=project_repository_replacement_requests,
        parent_table=projects,
        parent_fk=project_repository_replacement_requests.c.project_id,
        item_id_prefix="vote-project-repository-",
        vote_kind="repository_replacement",
        surface="projects",
        seen=seen,
        appended=appended,
        title_prefix="Repository replacement",
    )
    _append_link_votes(db, current_user_id, seen, appended)
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
                .join(
                    project_activities,
                    project_activities.c.id == project_activity_roles.c.activity_id,
                )
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
                .join(
                    event_activities,
                    event_activities.c.id == event_activity_roles.c.activity_id,
                )
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
