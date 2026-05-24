from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import project_memberships, project_service_requests, projects

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
