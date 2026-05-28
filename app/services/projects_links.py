from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_link_request_votes,
    project_link_requests,
    project_links,
    project_memberships,
    projects,
)
from app.utils.votes import required_votes, resolve_project_vote_population

APPROVAL_THRESHOLD = 0.66
VALID_VOTES = frozenset({"yes", "no"})


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
            detail="Only project members can perform this action",
        )


def _bilateral_vote_population(
    db: Session,
    source_row: Mapping[str, object],
    target_row: Mapping[str, object],
) -> int:
    source_pop = resolve_project_vote_population(
        db, source_row["id"], bool(source_row["is_platform_tagged"])
    )
    target_pop = resolve_project_vote_population(
        db, target_row["id"], bool(target_row["is_platform_tagged"])
    )
    return source_pop + target_pop


def _compute_vote_summary(
    db: Session, request_id: UUID, member_count: int
) -> dict[str, object]:
    rows = db.execute(
        select(project_link_request_votes.c.vote).where(
            project_link_request_votes.c.request_id == request_id
        )
    ).all()

    yes_count = sum(1 for (v,) in rows if v == "yes")
    no_count = sum(1 for (v,) in rows if v == "no")
    total_votes = yes_count + no_count
    approval_ratio = yes_count / total_votes if total_votes > 0 else 0.0
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


def _serialize_link_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "source_project_id": row["source_project_id"],
        "target_project_id": row["target_project_id"],
        "relationship_label": row["relationship_label"],
        "summary": row["summary"],
        "proposed_by": row["proposed_by"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def create_project_link_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_project_slug: str,
    relationship_label: str,
    summary: str,
) -> dict[str, object]:
    source_row = _get_project_by_slug(db, project_slug)
    target_row = _get_project_by_slug(db, target_project_slug)
    _ensure_member(db, source_row["id"], current_user_id)

    if source_row["id"] == target_row["id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot link a project to itself",
        )

    try:
        created = db.execute(
            insert(project_link_requests)
            .values(
                source_project_id=source_row["id"],
                target_project_id=target_row["id"],
                relationship_label=relationship_label.strip(),
                summary=summary.strip(),
                proposed_by=current_user_id,
                status="open",
            )
            .returning(
                project_link_requests.c.id,
                project_link_requests.c.source_project_id,
                project_link_requests.c.target_project_id,
                project_link_requests.c.relationship_label,
                project_link_requests.c.summary,
                project_link_requests.c.proposed_by,
                project_link_requests.c.status,
                project_link_requests.c.created_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create link request",
        ) from exc

    population = _bilateral_vote_population(db, source_row, target_row)
    return {
        "request": _serialize_link_request(
            created, _compute_vote_summary(db, created["id"], population)
        )
    }


def vote_project_link_request(
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

    request_row = db.execute(
        select(project_link_requests).where(project_link_requests.c.id == request_id)
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Link request is already closed")

    if project_row["id"] == request_row["source_project_id"]:
        vote_scope = "source"
    elif project_row["id"] == request_row["target_project_id"]:
        vote_scope = "target"
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project is not party to this link request",
        )

    existing_vote = db.execute(
        select(project_link_request_votes.c.vote).where(
            project_link_request_votes.c.request_id == request_id,
            project_link_request_votes.c.voter_id == current_user_id,
            project_link_request_votes.c.vote_scope == vote_scope,
        )
    ).first()

    source_row = db.execute(
        select(projects).where(projects.c.id == request_row["source_project_id"])
    ).mappings().first()
    target_row = db.execute(
        select(projects).where(projects.c.id == request_row["target_project_id"])
    ).mappings().first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_link_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                    vote_scope=vote_scope,
                )
            )
        else:
            db.execute(
                update(project_link_request_votes)
                .where(
                    project_link_request_votes.c.request_id == request_id,
                    project_link_request_votes.c.voter_id == current_user_id,
                    project_link_request_votes.c.vote_scope == vote_scope,
                )
                .values(vote=normalized_vote)
            )

        population = _bilateral_vote_population(db, source_row, target_row)
        summary_dict = _compute_vote_summary(db, request_id, population)
        executed = False

        if summary_dict["is_passing"]:
            db.execute(
                update(project_link_requests)
                .where(project_link_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                insert(project_links).values(
                    source_project_id=request_row["source_project_id"],
                    target_project_id=request_row["target_project_id"],
                    relationship_label=request_row["relationship_label"],
                    summary=request_row["summary"],
                    link_kind="manual",
                    status="active",
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

    refreshed_request = db.execute(
        select(project_link_requests).where(project_link_requests.c.id == request_id)
    ).mappings().one()
    refreshed_source = db.execute(
        select(projects).where(projects.c.id == request_row["source_project_id"])
    ).mappings().first()
    refreshed_target = db.execute(
        select(projects).where(projects.c.id == request_row["target_project_id"])
    ).mappings().first()
    final_population = _bilateral_vote_population(db, refreshed_source, refreshed_target)
    final_summary = _compute_vote_summary(db, request_id, final_population)

    return {
        "request": _serialize_link_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "vote_scope": vote_scope,
        "executed": executed,
    }
