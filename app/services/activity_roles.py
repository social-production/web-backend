"""Activity roles across events and projects: who's told when a role is taken, and
releasing someone's upcoming roles when they leave."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    users,
)
from app.services.notifications import create_notification

_TABLES = {
    "event": (event_activities, event_activity_roles, event_activity_assignments, "event_id"),
    "project": (
        project_activities,
        project_activity_roles,
        project_activity_assignments,
        "project_id",
    ),
}


def release_upcoming_roles(db: Session, *, kind: str, scope_id: UUID, user_id: UUID) -> int:
    """Drop the person from roles in activities that haven't ended. Past ones stay for history.

    Doesn't commit; returns how many roles were released.
    """
    activities, roles, assignments, fk = _TABLES[kind]
    role_ids = (
        db.execute(
            select(roles.c.id)
            .select_from(roles.join(activities, activities.c.id == roles.c.activity_id))
            .where(activities.c[fk] == scope_id, activities.c.ends_at > datetime.now(UTC))
        )
        .scalars()
        .all()
    )
    if not role_ids:
        return 0
    result = db.execute(
        delete(assignments).where(
            assignments.c.role_id.in_(role_ids), assignments.c.user_id == user_id
        )
    )
    return int(result.rowcount or 0)


def notify_role_taken(
    db: Session,
    *,
    kind: str,
    scope_id: UUID,
    slug: str,
    activity_row: Mapping[str, object],
    role_label: str,
    actor_id: UUID,
) -> None:
    """Tell whoever set up the activity that someone took a role in it."""
    author_id = activity_row.get("author_id")
    if author_id is None or author_id == actor_id:
        return
    name = db.execute(select(users.c.username).where(users.c.id == actor_id)).scalar_one_or_none()
    create_notification(
        db=db,
        recipient_id=author_id,
        actor_id=actor_id,
        kind="evt-role-taken" if kind == "event" else "prj-role-taken",
        surface="event" if kind == "event" else "project",
        subject_type=kind,
        subject_id=scope_id,
        target_id=activity_row["id"],
        title=str(activity_row["title"]),
        body=f'{name or "Someone"} took the "{role_label}" role in {activity_row["title"]}.',
        href=f"/{kind}s/{slug}?activity={activity_row['id']}",
    )


def notify_member_left(
    db: Session,
    *,
    kind: str,
    scope_id: UUID,
    slug: str,
    title: str,
    organizer_ids: list[UUID],
    actor_id: UUID,
    released_roles: int,
) -> None:
    """Tell organizers someone left, and whether that opened up roles."""
    name = db.execute(select(users.c.username).where(users.c.id == actor_id)).scalar_one_or_none()
    body = f"{name or 'Someone'} left {title}."
    if released_roles:
        body += f" Their {'role is' if released_roles == 1 else 'roles are'} open again."
    for organizer_id in organizer_ids:
        if organizer_id == actor_id:
            continue
        create_notification(
            db=db,
            recipient_id=organizer_id,
            actor_id=actor_id,
            kind="evt-member-left" if kind == "event" else "prj-member-left",
            surface=kind,
            subject_type=kind,
            subject_id=scope_id,
            title=title,
            body=body,
            href=f"/{kind}s/{slug}",
        )
