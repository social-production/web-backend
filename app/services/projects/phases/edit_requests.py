from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_edit_request_votes,
    project_edit_requests,
    projects,
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
from app.services.projects.phases.serializers import _serialize_edit_request
from app.services.search import index_document


def create_project_edit_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    description: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_governance_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)

    created = (
        db.execute(
            insert(project_edit_requests)
            .values(
                project_id=project_row["id"],
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                status="open",
            )
            .returning(
                project_edit_requests.c.id,
                project_edit_requests.c.project_id,
                project_edit_requests.c.title,
                project_edit_requests.c.description,
                project_edit_requests.c.author_id,
                project_edit_requests.c.status,
                project_edit_requests.c.created_at,
            )
        )
        .mappings()
        .one()
    )
    db.commit()
    summary = _compute_simple_vote_summary(
        db, project_edit_request_votes, created["id"], _project_vote_population(db, project_row)
    )
    return {"request": _serialize_edit_request(created, summary)}


def vote_project_edit_request(
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
            select(project_edit_requests).where(
                project_edit_requests.c.id == request_id,
                project_edit_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project edit request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Project edit request is already closed"
        )

    existing_vote = db.execute(
        select(project_edit_request_votes.c.vote).where(
            project_edit_request_votes.c.request_id == request_id,
            project_edit_request_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_edit_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_edit_request_votes)
                .where(
                    project_edit_request_votes.c.request_id == request_id,
                    project_edit_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_simple_vote_summary(
            db, project_edit_request_votes, request_id, _project_vote_population(db, project_row)
        )
        executed = False
        if summary["is_passing"]:
            db.execute(
                update(project_edit_requests)
                .where(project_edit_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(
                    title=request_row["title"],
                    description=request_row["description"],
                )
            )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(project_edit_requests)
                .where(project_edit_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record edit vote"
        ) from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "project-edit-request",
            "target_id": str(request_id),
            "vote": normalized_vote,
        },
    )

    refreshed_request = (
        db.execute(select(project_edit_requests).where(project_edit_requests.c.id == request_id))
        .mappings()
        .one()
    )
    refreshed_project = (
        db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    )
    final_summary = _compute_simple_vote_summary(
        db,
        project_edit_request_votes,
        request_id,
        _project_vote_population(db, project_row),
    )
    if executed:
        index_document(
            db=db,
            entity_type="project",
            entity_id=project_row["id"],
            title=str(refreshed_project["title"]),
            summary=str(refreshed_project["description"]),
            meta="project",
            href=f"/projects/{project_row['slug']}",
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not persist edit vote activity",
        ) from exc
    return {
        "request": _serialize_edit_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }
