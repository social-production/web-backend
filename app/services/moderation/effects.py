from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.models import (
    comments,
    events,
    help_requests,
    messages,
    notifications,
    posts,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_requests,
    project_memberships,
    project_phase_change_requests,
    project_update_requests,
    projects,
    searchable_documents,
    threads,
)
from app.services.moderation.thresholds import TOP_LEVEL_TARGET_TYPES, reason_label
from app.services.notification_preferences import recipient_allows_notification


def _table_for_target(target_type: str):
    return {
        "post": posts,
        "thread": threads,
        "project": projects,
        "event": events,
        "help_request": help_requests,
        "comment": comments,
        "message": messages,
    }.get(target_type)


def apply_moderation_state(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    moderation_state: str,
    moderation_reason: str | None,
) -> None:
    table = _table_for_target(target_type)
    if table is None:
        return
    db.execute(
        update(table)
        .where(table.c.id == target_id)
        .values(
            moderation_state=moderation_state,
            moderation_reason=moderation_reason,
        )
    )


def _unindex_search_document(db: Session, *, target_type: str, target_id: UUID) -> None:
    db.execute(
        delete(searchable_documents).where(
            searchable_documents.c.entity_type == target_type,
            searchable_documents.c.entity_id == target_id,
        )
    )


def apply_resolution_effect(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    reason: str,
    resolution: str,
) -> None:
    normalized = target_type.strip().lower()

    if resolution == "dismissed":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="visible",
            moderation_reason=None,
        )
        return

    if resolution == "under_review":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="under_review",
            moderation_reason=reason,
        )
        return

    if resolution == "hidden":
        # Serious-harm restriction applies on all surfaces: message-like bodies
        # hide/reveal, top-level content blurs with a warning until deletion.
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="hidden",
            moderation_reason=reason,
        )
        return

    if resolution == "removed":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="removed",
            moderation_reason=reason,
        )
        if normalized in TOP_LEVEL_TARGET_TYPES:
            _unindex_search_document(db, target_type=normalized, target_id=target_id)
        if normalized == "project":
            _end_removed_project(db, project_id=target_id, reason=reason)


def _end_removed_project(db: Session, *, project_id: UUID, reason: str) -> None:
    """End members, open votes, and role signups, then tell the people involved."""
    project_row = (
        db.execute(select(projects.c.title).where(projects.c.id == project_id)).mappings().first()
    )
    if project_row is None:
        return

    member_ids = set(
        db.execute(
            select(project_memberships.c.user_id).where(
                project_memberships.c.project_id == project_id
            )
        )
        .scalars()
        .all()
    )
    role_ids = select(project_activity_roles.c.id).where(
        project_activity_roles.c.activity_id.in_(
            select(project_activities.c.id).where(project_activities.c.project_id == project_id)
        )
    )
    member_ids.update(
        db.execute(
            select(project_activity_assignments.c.user_id).where(
                project_activity_assignments.c.role_id.in_(role_ids)
            )
        )
        .scalars()
        .all()
    )

    db.execute(
        delete(project_activity_assignments).where(
            project_activity_assignments.c.role_id.in_(role_ids)
        )
    )
    for request_table in (
        project_phase_change_requests,
        project_edit_requests,
        project_update_requests,
    ):
        db.execute(
            update(request_table)
            .where(request_table.c.project_id == project_id, request_table.c.status == "open")
            .values(status="closed")
        )
    db.execute(delete(project_memberships).where(project_memberships.c.project_id == project_id))

    title = str(project_row["title"])
    for member_id in member_ids:
        if member_id is None or not recipient_allows_notification(db, member_id, "prj-removed"):
            continue
        db.execute(
            insert(notifications).values(
                recipient_id=member_id,
                actor_id=None,
                kind="prj-removed",
                surface="project",
                subject_type="project",
                subject_id=project_id,
                target_id=None,
                title=title,
                body=f"This project was removed after a {reason_label(reason)} report.",
                href="/",
                is_unread=True,
                read_at=None,
            )
        )
