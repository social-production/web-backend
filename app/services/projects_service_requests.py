from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_activities,
    project_activity_roles,
    project_memberships,
    project_service_history_completions,
    project_service_requests,
    projects,
)

VALID_SERVICE_REQUEST_STATUS = frozenset({"open", "planned", "accepted", "declined"})


def _serialize_service_request(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "requester_id": row["requester_id"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "scheduled_at": row["scheduled_at"],
        "ends_at": row["ends_at"],
        "linked_activity_id": row["linked_activity_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_project_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


def _ensure_collective_service(project_mode: str) -> None:
    if project_mode != "collective-service":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only collective-service projects support service requests",
        )


def _ensure_project_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can update request status")


def create_service_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    body: str,
    scheduled_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_collective_service(project_row["project_mode"])

    try:
        created = db.execute(
            insert(project_service_requests)
            .values(
                project_id=project_row["id"],
                requester_id=current_user_id,
                title=title.strip(),
                body=body.strip(),
                status="open",
                scheduled_at=scheduled_at,
                ends_at=ends_at,
                linked_activity_id=None,
            )
            .returning(
                project_service_requests.c.id,
                project_service_requests.c.project_id,
                project_service_requests.c.requester_id,
                project_service_requests.c.title,
                project_service_requests.c.body,
                project_service_requests.c.status,
                project_service_requests.c.scheduled_at,
                project_service_requests.c.ends_at,
                project_service_requests.c.linked_activity_id,
                project_service_requests.c.created_at,
                project_service_requests.c.updated_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create service request") from exc

    return {"request": _serialize_service_request(created)}


def list_service_requests(db: Session, project_slug: str, status_filter: str | None = None) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_collective_service(project_row["project_mode"])

    query = select(project_service_requests).where(project_service_requests.c.project_id == project_row["id"])

    if status_filter:
        normalized = status_filter.strip().lower()
        if normalized not in VALID_SERVICE_REQUEST_STATUS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of: {sorted(VALID_SERVICE_REQUEST_STATUS)}",
            )
        query = query.where(project_service_requests.c.status == normalized)

    rows = db.execute(query.order_by(project_service_requests.c.created_at.desc())).mappings().all()
    items = [_serialize_service_request(row) for row in rows]

    return {
        "project_slug": project_row["slug"],
        "project_mode": project_row["project_mode"],
        "total": len(items),
        "items": items,
    }


def update_service_request_status(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    status_value: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_collective_service(project_row["project_mode"])
    _ensure_project_member(db, project_row["id"], current_user_id)

    normalized_status = status_value.strip().lower()
    if normalized_status not in VALID_SERVICE_REQUEST_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of: {sorted(VALID_SERVICE_REQUEST_STATUS)}",
        )

    request_row = db.execute(
        select(project_service_requests).where(
            project_service_requests.c.id == request_id,
            project_service_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service request not found")

    try:
        db.execute(
            update(project_service_requests)
            .where(project_service_requests.c.id == request_id)
            .values(status=normalized_status)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update request status") from exc

    refreshed = db.execute(
        select(project_service_requests).where(project_service_requests.c.id == request_id)
    ).mappings().one()

    return {"request": _serialize_service_request(refreshed)}


def plan_service_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    title: str,
    location_label: str,
    role_requirements: list[dict[str, object]],
    linked_plan_phase_id: str | None,
    note: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_collective_service(project_row["project_mode"])
    _ensure_project_member(db, project_row["id"], current_user_id)

    request_row = db.execute(
        select(project_service_requests).where(
            project_service_requests.c.id == request_id,
            project_service_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service request not found")
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service request is not open",
        )

    scheduled_at = request_row["scheduled_at"]
    ends_at = request_row["ends_at"]
    if scheduled_at is None or ends_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Service request must have scheduled_at and ends_at to be planned",
        )
    if ends_at <= scheduled_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ends_at must be after scheduled_at",
        )

    try:
        created_activity = db.execute(
            insert(project_activities)
            .values(
                project_id=project_row["id"],
                linked_plan_id=None,
                linked_plan_phase_id=linked_plan_phase_id,
                linked_request_id=request_id,
                title=title.strip(),
                author_id=current_user_id,
                scheduled_at=scheduled_at,
                ends_at=ends_at,
                location_label=location_label.strip(),
                note=note.strip(),
                status="active",
            )
            .returning(
                project_activities.c.id,
                project_activities.c.project_id,
                project_activities.c.title,
                project_activities.c.author_id,
                project_activities.c.scheduled_at,
                project_activities.c.ends_at,
                project_activities.c.location_label,
                project_activities.c.note,
                project_activities.c.linked_plan_phase_id,
                project_activities.c.linked_request_id,
                project_activities.c.status,
                project_activities.c.created_at,
            )
        ).mappings().one()

        role_items = []
        for req in role_requirements:
            label = str(req.get("label", "")).strip()
            required_count = int(req.get("required_count", 0))
            maximum_count_raw = req.get("maximum_count")
            maximum_count = int(maximum_count_raw) if maximum_count_raw is not None else None
            if not label or required_count < 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid role requirement",
                )
            if maximum_count is not None and maximum_count < required_count:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="maximum_count must be >= required_count",
                )
            role = db.execute(
                insert(project_activity_roles)
                .values(
                    activity_id=created_activity["id"],
                    label=label,
                    required_count=required_count,
                    maximum_count=maximum_count,
                )
                .returning(
                    project_activity_roles.c.id,
                    project_activity_roles.c.label,
                    project_activity_roles.c.required_count,
                    project_activity_roles.c.maximum_count,
                )
            ).mappings().one()
            role_items.append(dict(role))

        db.execute(
            update(project_service_requests)
            .where(project_service_requests.c.id == request_id)
            .values(status="planned", linked_activity_id=created_activity["id"])
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not plan service request",
        ) from exc
    except HTTPException:
        db.rollback()
        raise

    refreshed_request = db.execute(
        select(project_service_requests).where(project_service_requests.c.id == request_id)
    ).mappings().one()

    return {
        "request": _serialize_service_request(refreshed_request),
        "activity": {**dict(created_activity), "roles": role_items},
    }


def toggle_service_history_completion(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    history_item_key: str,
    role: str,
    selection: str | None,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_collective_service(project_row["project_mode"])

    normalized_role = role.strip().lower()
    if normalized_role not in {"requester", "participants"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role must be 'requester' or 'participants'",
        )

    normalized_selection = selection.strip().lower() if selection else None
    if normalized_selection is not None and normalized_selection not in {"completed", "uncompleted"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="selection must be 'completed', 'uncompleted', or null",
        )

    requester_user_id = current_user_id if normalized_role == "requester" else None
    participant_user_id = current_user_id if normalized_role == "participants" else None

    existing = db.execute(
        select(project_service_history_completions).where(
            project_service_history_completions.c.project_id == project_row["id"],
            project_service_history_completions.c.history_item_key == history_item_key,
            project_service_history_completions.c.role == normalized_role,
            project_service_history_completions.c.requester_user_id == requester_user_id,
            project_service_history_completions.c.participant_user_id == participant_user_id,
        )
    ).mappings().first()

    try:
        if normalized_selection is None:
            if existing is not None:
                db.execute(
                    delete(project_service_history_completions).where(
                        project_service_history_completions.c.project_id == project_row["id"],
                        project_service_history_completions.c.history_item_key == history_item_key,
                        project_service_history_completions.c.role == normalized_role,
                        project_service_history_completions.c.requester_user_id == requester_user_id,
                        project_service_history_completions.c.participant_user_id == participant_user_id,
                    )
                )
        elif existing is None:
            db.execute(
                insert(project_service_history_completions).values(
                    project_id=project_row["id"],
                    history_item_key=history_item_key,
                    requester_user_id=requester_user_id,
                    participant_user_id=participant_user_id,
                    role=normalized_role,
                    completion_state=normalized_selection,
                )
            )
        else:
            db.execute(
                update(project_service_history_completions)
                .where(
                    project_service_history_completions.c.project_id == project_row["id"],
                    project_service_history_completions.c.history_item_key == history_item_key,
                    project_service_history_completions.c.role == normalized_role,
                    project_service_history_completions.c.requester_user_id == requester_user_id,
                    project_service_history_completions.c.participant_user_id == participant_user_id,
                )
                .values(completion_state=normalized_selection)
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not update completion state",
        ) from exc

    return {
        "ok": True,
        "history_item_key": history_item_key,
        "role": normalized_role,
        "selection": normalized_selection,
    }
