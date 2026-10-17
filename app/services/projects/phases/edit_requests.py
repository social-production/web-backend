from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
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
    _ensure_can_cast_governance_vote,
    _ensure_governance_requests_allowed,
    _ensure_member,
    _get_project_by_slug,
    _project_vote_population,
)
from app.services.projects.phases.serializers import _serialize_edit_request
from app.services.proposer_vote import cast_proposer_yes
from app.services.search import index_document
from app.utils.edits import changed_text


def create_project_edit_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    description: str,
    location_label: str | None = None,
    location_id: UUID | None = None,
    is_online: bool = False,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_governance_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)

    proposed_location_label = None
    proposed_location_id = None
    cleaned_location = (location_label or "").strip()
    if cleaned_location and (
        cleaned_location != str(project_row["location_label"] or "")
        or location_id != project_row.get("location_id")
    ):
        from app.services.locations.resolve import ensure_location_id

        proposed_location_id, proposed_location_label = ensure_location_id(
            db,
            location_id=location_id,
            location_label=cleaned_location,
            is_online=is_online,
        )

    # Store only what changes, so two passed edits never undo each other.
    proposed_title = changed_text(title, project_row["title"])
    proposed_description = changed_text(description, project_row["description"])
    if proposed_title is None and proposed_description is None and proposed_location_label is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing has changed"
        )

    created = (
        db.execute(
            insert(project_edit_requests)
            .values(
                project_id=project_row["id"],
                title=proposed_title,
                description=proposed_description,
                location_label=proposed_location_label,
                location_id=proposed_location_id,
                author_id=current_user_id,
                status="open",
            )
            .returning(
                project_edit_requests.c.id,
                project_edit_requests.c.project_id,
                project_edit_requests.c.title,
                project_edit_requests.c.description,
                project_edit_requests.c.location_label,
                project_edit_requests.c.author_id,
                project_edit_requests.c.status,
                project_edit_requests.c.created_at,
            )
        )
        .mappings()
        .one()
    )
    db.commit()
    voted = cast_proposer_yes(
        lambda vote: vote_project_edit_request(
            db, current_user_id, project_slug, created["id"], vote
        )
    )
    if voted is not None:
        return {"request": voted["request"], "executed": voted["executed"]}

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
    _ensure_can_cast_governance_vote(db, project_row, current_user_id)

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
        if normalized_vote == "neutral":
            if existing_vote is not None:
                db.execute(
                    delete(project_edit_request_votes).where(
                        project_edit_request_votes.c.request_id == request_id,
                        project_edit_request_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing_vote is None:
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
            applied_values = {
                field: request_row[field]
                for field in ("title", "description")
                if request_row[field] is not None
            }
            if request_row.get("location_label"):
                applied_values["location_label"] = request_row["location_label"]
                applied_values["location_id"] = request_row.get("location_id")
            if applied_values:
                db.execute(
                    update(projects)
                    .where(projects.c.id == project_row["id"])
                    .values(**applied_values)
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
