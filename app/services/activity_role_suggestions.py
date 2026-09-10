from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    event_activities,
    event_activity_roles,
    event_memberships,
    events,
    project_activities,
    project_activity_roles,
    project_memberships,
    projects,
    users,
)
from app.services.notifications import create_notification


def _user_summary(db: Session, user_id: UUID | None) -> dict[str, object] | None:
    if user_id is None:
        return None
    row = (
        db.execute(select(users.c.id, users.c.username).where(users.c.id == user_id))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {"id": str(row["id"]), "username": row["username"]}


def serialize_role_suggestion(
    role: Mapping[str, object], usernames: dict[UUID, Mapping[str, object]] | None = None
) -> dict[str, object]:
    suggested_user_id = role.get("suggested_user_id")
    status_value = role.get("suggestion_status")
    suggested_user = None
    if suggested_user_id is not None:
        if usernames is not None:
            info = usernames.get(suggested_user_id)
            if info is not None:
                suggested_user = {
                    "id": str(suggested_user_id),
                    "username": info.get("username", "unknown"),
                }
        if suggested_user is None:
            suggested_user = {"id": str(suggested_user_id), "username": "unknown"}
    return {
        "suggestedUser": suggested_user if status_value != "declined" else None,
        "suggestionStatus": status_value,
        "suggestedByUserId": str(role["suggested_by_user_id"])
        if role.get("suggested_by_user_id")
        else None,
    }


def _parse_suggested_user_id(raw: object) -> UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="suggested_user_id must be a UUID",
        ) from exc


def notify_role_suggestion(
    db: Session,
    *,
    recipient_id: UUID,
    actor_id: UUID,
    kind: str,
    subject_type: str,
    subject_id: UUID,
    activity_id: UUID,
    title: str,
    body: str,
    href: str,
) -> None:
    create_notification(
        db,
        recipient_id=recipient_id,
        actor_id=actor_id,
        kind=kind,
        surface="inbox",
        subject_type=subject_type,
        subject_id=subject_id,
        target_id=activity_id,
        title=title,
        body=body,
        href=href,
    )


def suggest_project_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_id: UUID,
    suggested_user_id: UUID,
) -> dict[str, object]:
    from app.services.projects.helpers import _get_project_by_slug_row

    project_row = _get_project_by_slug_row(db, slug)
    membership = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_row["id"],
            project_memberships.c.user_id == current_user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can suggest a role"
        )
    if suggested_user_id == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Suggest someone else for this role",
        )
    suggested = _user_summary(db, suggested_user_id)
    if suggested is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    activity = (
        db.execute(
            select(project_activities).where(
                project_activities.c.id == activity_id,
                project_activities.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if activity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    role = (
        db.execute(
            select(project_activity_roles).where(
                project_activity_roles.c.id == role_id,
                project_activity_roles.c.activity_id == activity_id,
            )
        )
        .mappings()
        .first()
    )
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    db.execute(
        update(project_activity_roles)
        .where(project_activity_roles.c.id == role_id)
        .values(
            suggested_user_id=suggested_user_id,
            suggested_by_user_id=current_user_id,
            suggestion_status="pending",
        )
    )
    href = f"/projects/{project_row['slug']}?activity={activity_id}"
    notify_role_suggestion(
        db,
        recipient_id=suggested_user_id,
        actor_id=current_user_id,
        kind="prj-role-suggest",
        subject_type="project",
        subject_id=project_row["id"],
        activity_id=activity_id,
        title=f"Suggested for {role['label']}",
        body=f"You've been suggested for {role['label']} on {activity['title']}.",
        href=href,
    )
    db.commit()
    return {"ok": True, "suggestedUser": suggested}


def decline_project_activity_role_suggestion(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_id: UUID,
) -> dict[str, object]:
    project_row = (
        db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    )
    if project_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    role = (
        db.execute(
            select(project_activity_roles).where(
                project_activity_roles.c.id == role_id,
                project_activity_roles.c.activity_id == activity_id,
            )
        )
        .mappings()
        .first()
    )
    if role is None or role.get("suggested_user_id") != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the suggested user can decline"
        )
    suggester_id = role.get("suggested_by_user_id")
    db.execute(
        update(project_activity_roles)
        .where(project_activity_roles.c.id == role_id)
        .values(suggested_user_id=None, suggested_by_user_id=None, suggestion_status="declined")
    )
    if suggester_id is not None:
        notify_role_suggestion(
            db,
            recipient_id=suggester_id,
            actor_id=current_user_id,
            kind="prj-role-suggest",
            subject_type="project",
            subject_id=project_row["id"],
            activity_id=activity_id,
            title=f"{role['label']} suggestion declined",
            body="The person you suggested declined this role.",
            href=f"/projects/{project_row['slug']}?activity={activity_id}",
        )
    db.commit()
    return {"ok": True}


def suggest_event_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_id: UUID,
    suggested_user_id: UUID,
) -> dict[str, object]:
    from app.services.events.helpers import _get_event_by_slug_row

    event_row = _get_event_by_slug_row(db, slug)
    membership = db.execute(
        select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == event_row["id"],
            event_memberships.c.user_id == current_user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only event members can suggest a role"
        )
    suggested = _user_summary(db, suggested_user_id)
    if suggested is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    activity = (
        db.execute(
            select(event_activities).where(
                event_activities.c.id == activity_id,
                event_activities.c.event_id == event_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if activity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    role = (
        db.execute(
            select(event_activity_roles).where(
                event_activity_roles.c.id == role_id,
                event_activity_roles.c.activity_id == activity_id,
            )
        )
        .mappings()
        .first()
    )
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    db.execute(
        update(event_activity_roles)
        .where(event_activity_roles.c.id == role_id)
        .values(
            suggested_user_id=suggested_user_id,
            suggested_by_user_id=current_user_id,
            suggestion_status="pending",
        )
    )
    notify_role_suggestion(
        db,
        recipient_id=suggested_user_id,
        actor_id=current_user_id,
        kind="evt-role-suggest",
        subject_type="event",
        subject_id=event_row["id"],
        activity_id=activity_id,
        title=f"Suggested for {role['label']}",
        body=f"You've been suggested for {role['label']} on {activity['title']}.",
        href=f"/events/{event_row['slug']}?activity={activity_id}",
    )
    db.commit()
    return {"ok": True, "suggestedUser": suggested}


def decline_event_activity_role_suggestion(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_id: UUID,
) -> dict[str, object]:
    event_row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if event_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    role = (
        db.execute(
            select(event_activity_roles).where(
                event_activity_roles.c.id == role_id,
                event_activity_roles.c.activity_id == activity_id,
            )
        )
        .mappings()
        .first()
    )
    if role is None or role.get("suggested_user_id") != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the suggested user can decline"
        )
    suggester_id = role.get("suggested_by_user_id")
    db.execute(
        update(event_activity_roles)
        .where(event_activity_roles.c.id == role_id)
        .values(suggested_user_id=None, suggested_by_user_id=None, suggestion_status="declined")
    )
    if suggester_id is not None:
        notify_role_suggestion(
            db,
            recipient_id=suggester_id,
            actor_id=current_user_id,
            kind="evt-role-suggest",
            subject_type="event",
            subject_id=event_row["id"],
            activity_id=activity_id,
            title=f"{role['label']} suggestion declined",
            body="The person you suggested declined this role.",
            href=f"/events/{event_row['slug']}?activity={activity_id}",
        )
    db.commit()
    return {"ok": True}


def role_insert_suggestion_values(
    req: dict[str, object], current_user_id: UUID
) -> dict[str, object]:
    suggested_user_id = _parse_suggested_user_id(req.get("suggested_user_id"))
    if suggested_user_id is None:
        return {
            "suggested_user_id": None,
            "suggested_by_user_id": None,
            "suggestion_status": None,
        }
    return {
        "suggested_user_id": suggested_user_id,
        "suggested_by_user_id": current_user_id,
        "suggestion_status": "pending",
    }
