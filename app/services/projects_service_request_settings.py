from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_service_request_setting_change_votes,
    project_service_request_setting_changes,
    project_service_request_settings,
    projects,
)
from app.services.governance_votes import compute_vote_summary
from app.utils.votes import resolve_project_vote_population

APPROVAL_THRESHOLD = 0.66
VALID_VOTES = frozenset({"yes", "no"})
VALID_REQUEST_MODES = frozenset({"calendar", "direct", "both"})


def _get_project_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


def _ensure_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only project members can request or vote",
        )


def _project_vote_population(db: Session, project_row: Mapping[str, object]) -> int:
    return resolve_project_vote_population(
        db, project_row["id"], bool(project_row["is_platform_tagged"])
    )


def _compute_vote_summary(db: Session, request_id: UUID, member_count: int) -> dict[str, object]:
    return compute_vote_summary(
        db, project_service_request_setting_change_votes, request_id, member_count
    )


def _serialize_change_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "author_id": row["author_id"],
        "reason": row["reason"],
        "enabled": row["enabled"],
        "request_mode": row["request_mode"],
        "allow_off_schedule_requests": row["allow_off_schedule_requests"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def create_settings_change_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    reason: str,
    enabled: bool,
    request_mode: str,
    allow_off_schedule_requests: bool,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_mode = request_mode.strip().lower()
    if normalized_mode not in VALID_REQUEST_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"request_mode must be one of: {sorted(VALID_REQUEST_MODES)}",
        )

    if project_row["project_mode"] == "personal-service":
        if project_row["author_id"] != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the service creator can update personal service request settings",
            )

        try:
            db.execute(
                pg_insert(project_service_request_settings)
                .values(
                    project_id=project_row["id"],
                    enabled=enabled,
                    request_mode=normalized_mode,
                    allow_off_schedule_requests=allow_off_schedule_requests,
                )
                .on_conflict_do_update(
                    index_elements=["project_id"],
                    set_={
                        "enabled": enabled,
                        "request_mode": normalized_mode,
                        "allow_off_schedule_requests": allow_off_schedule_requests,
                    },
                )
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not update request settings",
            ) from exc

        return {
            "applied": True,
            "settings": {
                "enabled": enabled,
                "request_mode": normalized_mode,
                "allow_off_schedule_requests": allow_off_schedule_requests,
            },
        }

    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reason is required for governed request settings changes",
        )

    try:
        created = (
            db.execute(
                insert(project_service_request_setting_changes)
                .values(
                    project_id=project_row["id"],
                    author_id=current_user_id,
                    reason=reason.strip(),
                    enabled=enabled,
                    request_mode=normalized_mode,
                    allow_off_schedule_requests=allow_off_schedule_requests,
                    status="open",
                )
                .returning(
                    project_service_request_setting_changes.c.id,
                    project_service_request_setting_changes.c.project_id,
                    project_service_request_setting_changes.c.author_id,
                    project_service_request_setting_changes.c.reason,
                    project_service_request_setting_changes.c.enabled,
                    project_service_request_setting_changes.c.request_mode,
                    project_service_request_setting_changes.c.allow_off_schedule_requests,
                    project_service_request_setting_changes.c.status,
                    project_service_request_setting_changes.c.created_at,
                )
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create settings change request",
        ) from exc

    summary = _compute_vote_summary(db, created["id"], _project_vote_population(db, project_row))
    return {"request": _serialize_change_request(created, summary)}


def vote_settings_change_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    request_row = (
        db.execute(
            select(project_service_request_setting_changes).where(
                project_service_request_setting_changes.c.id == request_id,
                project_service_request_setting_changes.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Settings change request not found",
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Settings change request is already closed",
        )

    existing_vote = db.execute(
        select(project_service_request_setting_change_votes.c.vote).where(
            project_service_request_setting_change_votes.c.request_id == request_id,
            project_service_request_setting_change_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_service_request_setting_change_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_service_request_setting_change_votes)
                .where(
                    project_service_request_setting_change_votes.c.request_id == request_id,
                    project_service_request_setting_change_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_vote_summary(db, request_id, _project_vote_population(db, project_row))
        executed = False

        if summary["is_passing"]:
            db.execute(
                update(project_service_request_setting_changes)
                .where(project_service_request_setting_changes.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                pg_insert(project_service_request_settings)
                .values(
                    project_id=project_row["id"],
                    enabled=request_row["enabled"],
                    request_mode=request_row["request_mode"],
                    allow_off_schedule_requests=request_row["allow_off_schedule_requests"],
                )
                .on_conflict_do_update(
                    index_elements=["project_id"],
                    set_={
                        "enabled": request_row["enabled"],
                        "request_mode": request_row["request_mode"],
                        "allow_off_schedule_requests": request_row["allow_off_schedule_requests"],
                    },
                )
            )
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not record vote",
        ) from exc

    refreshed_request = (
        db.execute(
            select(project_service_request_setting_changes).where(
                project_service_request_setting_changes.c.id == request_id
            )
        )
        .mappings()
        .one()
    )
    refreshed_project = (
        db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    )
    final_summary = _compute_vote_summary(
        db, request_id, _project_vote_population(db, refreshed_project)
    )

    return {
        "request": _serialize_change_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }
