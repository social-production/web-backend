from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    projects,
)
from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66
VALID_PHASE_IDS = frozenset({"phase-1", "phase-2", "phase-3", "phase-4", "phase-5", "phase-6", "phase-7"})
VALID_VOTES = frozenset({"yes", "no"})


def _serialize_phase_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "from_phase_id": row["from_phase_id"],
        "target_phase_id": row["target_phase_id"],
        "change_kind": row["change_kind"],
        "close_outcome": row["close_outcome"],
        "conversion_target_mode": row["conversion_target_mode"],
        "conversion_target_subtype": row["conversion_target_subtype"],
        "reason": row["reason"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can request or vote")


def _ensure_phase_requests_allowed(project_mode: str) -> None:
    if project_mode == "personal-service":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="personal-service projects do not allow phase change requests",
        )


def _compute_vote_summary(db: Session, request_id: UUID, member_count: int) -> dict[str, object]:
    rows = db.execute(
        select(project_phase_change_votes.c.vote).where(project_phase_change_votes.c.request_id == request_id)
    ).all()

    yes_count = 0
    no_count = 0
    for (vote,) in rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1

    total_votes = yes_count + no_count
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    votes_required = required_votes(member_count)
    meets_quorum = total_votes >= votes_required
    meets_approval = approval_ratio >= APPROVAL_THRESHOLD

    return {
        "yes_count": yes_count,
        "no_count": no_count,
        "total_votes": total_votes,
        "approval_ratio": approval_ratio,
        "approval_threshold": APPROVAL_THRESHOLD,
        "votes_required": votes_required,
        "member_count": member_count,
        "meets_quorum": meets_quorum,
        "meets_approval": meets_approval,
        "is_passing": meets_quorum and meets_approval,
    }


def create_phase_change_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_phase_id: str,
    reason: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_phase_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_target = target_phase_id.strip().lower()
    if normalized_target not in VALID_PHASE_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_phase_id must be one of: {sorted(VALID_PHASE_IDS)}",
        )

    current_phase_id = project_row["current_phase_id"]
    if normalized_target == current_phase_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_phase_id must differ from current_phase_id",
        )

    try:
        created = db.execute(
            insert(project_phase_change_requests)
            .values(
                project_id=project_row["id"],
                from_phase_id=current_phase_id,
                target_phase_id=normalized_target,
                change_kind="advance",
                close_outcome=None,
                conversion_target_mode=None,
                conversion_target_subtype=None,
                reason=reason.strip(),
                author_id=current_user_id,
                status="open",
            )
            .returning(
                project_phase_change_requests.c.id,
                project_phase_change_requests.c.project_id,
                project_phase_change_requests.c.from_phase_id,
                project_phase_change_requests.c.target_phase_id,
                project_phase_change_requests.c.change_kind,
                project_phase_change_requests.c.close_outcome,
                project_phase_change_requests.c.conversion_target_mode,
                project_phase_change_requests.c.conversion_target_subtype,
                project_phase_change_requests.c.reason,
                project_phase_change_requests.c.author_id,
                project_phase_change_requests.c.status,
                project_phase_change_requests.c.created_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create phase request") from exc

    summary = _compute_vote_summary(db, created["id"], int(project_row["member_count"] or 0))
    return {"request": _serialize_phase_request(created, summary)}


def list_phase_change_requests(db: Session, project_slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_phase_requests_allowed(project_row["project_mode"])

    member_count = int(project_row["member_count"] or 0)
    rows = db.execute(
        select(project_phase_change_requests)
        .where(project_phase_change_requests.c.project_id == project_row["id"])
        .order_by(project_phase_change_requests.c.created_at.desc())
    ).mappings().all()

    items = []
    for row in rows:
        summary = _compute_vote_summary(db, row["id"], member_count)
        items.append(_serialize_phase_request(row, summary))

    return {
        "project_slug": project_row["slug"],
        "project_mode": project_row["project_mode"],
        "current_phase_id": project_row["current_phase_id"],
        "total": len(items),
        "items": items,
    }


def vote_phase_change_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_phase_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    request_row = db.execute(
        select(project_phase_change_requests).where(
            project_phase_change_requests.c.id == request_id,
            project_phase_change_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phase change request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phase change request is already closed")

    existing_vote = db.execute(
        select(project_phase_change_votes.c.vote).where(
            project_phase_change_votes.c.request_id == request_id,
            project_phase_change_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_phase_change_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_phase_change_votes)
                .where(
                    project_phase_change_votes.c.request_id == request_id,
                    project_phase_change_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_vote_summary(db, request_id, int(project_row["member_count"] or 0))

        executed = False
        if summary["is_passing"]:
            db.execute(
                update(project_phase_change_requests)
                .where(project_phase_change_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(current_phase_id=request_row["target_phase_id"])
            )
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote") from exc

    refreshed_request = db.execute(
        select(project_phase_change_requests).where(project_phase_change_requests.c.id == request_id)
    ).mappings().one()
    refreshed_project = db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    final_summary = _compute_vote_summary(db, request_id, int(refreshed_project["member_count"] or 0))

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_project["current_phase_id"],
    }
