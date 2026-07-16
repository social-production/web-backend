from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_update_request_votes,
    project_update_requests,
    project_updates,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.projects.phases.constants import VALID_VOTES
from app.services.projects.phases.gates import (
    _compute_simple_vote_summary,
    _ensure_governance_requests_allowed,
    _ensure_member,
    _get_project_by_slug,
    _project_vote_population,
)
from app.services.projects.phases.serializers import _serialize_update_request


def create_project_update_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    body: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_governance_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)
    member_count = _project_vote_population(db, project_row)

    try:
        created = (
            db.execute(
                insert(project_update_requests)
                .values(
                    project_id=project_row["id"],
                    body=body.strip(),
                    author_id=current_user_id,
                    status="open",
                )
                .returning(
                    project_update_requests.c.id,
                    project_update_requests.c.project_id,
                    project_update_requests.c.body,
                    project_update_requests.c.author_id,
                    project_update_requests.c.status,
                    project_update_requests.c.created_at,
                )
            )
            .mappings()
            .one()
        )

        executed = False
        if member_count <= 1:
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == created["id"])
                .values(status="approved")
            )
            db.execute(
                insert(project_updates).values(
                    project_id=project_row["id"],
                    title="Approved update request",
                    body=created["body"],
                    author_id=created["author_id"],
                )
            )
            created = {**created, "status": "approved"}
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create update request",
        ) from exc

    summary = _compute_simple_vote_summary(
        db, project_update_request_votes, created["id"], member_count
    )
    return {"request": _serialize_update_request(created, summary), "executed": executed}


def vote_project_update_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_governance_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    request_row = (
        db.execute(
            select(project_update_requests).where(
                project_update_requests.c.id == request_id,
                project_update_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project update request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Project update request is already closed"
        )

    existing_vote = db.execute(
        select(project_update_request_votes.c.vote).where(
            project_update_request_votes.c.request_id == request_id,
            project_update_request_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_update_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_update_request_votes)
                .where(
                    project_update_request_votes.c.request_id == request_id,
                    project_update_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_simple_vote_summary(
            db, project_update_request_votes, request_id, _project_vote_population(db, project_row)
        )
        executed = False
        if summary["is_passing"]:
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                insert(project_updates).values(
                    project_id=project_row["id"],
                    title="Community update",
                    body=request_row["body"],
                    author_id=request_row["author_id"],
                )
            )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record update vote"
        ) from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "project-update-request",
            "target_id": str(request_id),
            "vote": normalized_vote,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not persist update vote activity",
        ) from exc

    refreshed_request = (
        db.execute(
            select(project_update_requests).where(project_update_requests.c.id == request_id)
        )
        .mappings()
        .one()
    )
    final_summary = _compute_simple_vote_summary(
        db,
        project_update_request_votes,
        request_id,
        _project_vote_population(db, project_row),
    )
    return {
        "request": _serialize_update_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }
