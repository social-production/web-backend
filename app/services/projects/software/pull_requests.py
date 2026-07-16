from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_pull_request_votes,
    project_pull_requests,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.projects.software.constants import VALID_VOTES
from app.services.projects.software.governance import _compute_vote_summary, _governance_payload
from app.services.projects.software.helpers import (
    _ensure_member,
    _ensure_software_tables,
    _get_project_by_slug,
    _is_merge_capable,
    _vote_rows,
)
from app.utils.votes import resolve_project_vote_population


def submit_pull_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    summary: str,
    pull_request_id: str,
    pull_request_url: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    try:
        db.execute(
            insert(project_pull_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                title=title.strip(),
                summary=summary.strip(),
                pull_request_id=pull_request_id.strip(),
                pull_request_url=pull_request_url.strip(),
                author_id=current_user_id,
                stage="approval",
                merge_id=None,
                merged_by_user_id=None,
                approval_threshold_percent=Decimal("66.00"),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not submit pull request",
        ) from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_pull_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="vote must be one of: ['no', 'yes']",
        )

    request_row = (
        db.execute(
            select(project_pull_requests).where(
                project_pull_requests.c.id == request_id,
                project_pull_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")
    if request_row["stage"] not in {"approval", "confirmation"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Pull request is not open for voting"
        )

    existing = (
        db.execute(
            select(project_pull_request_votes).where(
                project_pull_request_votes.c.request_id == request_id,
                project_pull_request_votes.c.voter_id == current_user_id,
            )
        )
        .mappings()
        .first()
    )

    try:
        if existing is None:
            db.execute(
                insert(project_pull_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_pull_request_votes)
                .where(
                    project_pull_request_votes.c.request_id == request_id,
                    project_pull_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_pull_request_votes, request_id),
            resolve_project_vote_population(
                db, project_row["id"], bool(project_row.get("is_platform_tagged"))
            ),
            current_user_id,
        )

        next_stage = request_row["stage"]
        previous_stage = request_row["stage"]
        if request_row["stage"] == "approval":
            if passes:
                next_stage = "awaiting-merge"
            elif not can_still_pass:
                next_stage = "rejected"
        elif request_row["stage"] == "confirmation":
            if passes:
                next_stage = "confirmed"
            elif not can_still_pass:
                next_stage = "awaiting-merge"

        if next_stage != request_row["stage"]:
            db.execute(
                update(project_pull_requests)
                .where(project_pull_requests.c.id == request_id)
                .values(stage=next_stage)
            )
            if previous_stage == "confirmation" and next_stage == "awaiting-merge":
                db.execute(
                    update(project_pull_requests)
                    .where(project_pull_requests.c.id == request_id)
                    .values(merge_id=None, merged_by_user_id=None)
                )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "pull-request",
                "target_id": str(request_id),
                "vote": normalized_vote,
            },
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record vote"
        ) from exc

    if (
        previous_stage == "approval"
        and next_stage == "awaiting-merge"
        and request_row["author_id"] is not None
    ):
        create_notification(
            db=db,
            recipient_id=request_row["author_id"],
            actor_id=current_user_id,
            kind="pr-approved",
            surface="project",
            subject_type="pull-request",
            subject_id=request_id,
            target_id=project_row["id"],
            title="Pull request approved",
            body="Voting passed and your pull request is approved for merge.",
            href=f"/projects/{project_row['slug']}/software",
        )

    return _governance_payload(db, project_row, current_user_id)


def record_pull_request_merge(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    merge_id: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    if not _is_merge_capable(db, project_row["id"], current_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only members with merge capability can record merges",
        )

    request_row = (
        db.execute(
            select(project_pull_requests).where(
                project_pull_requests.c.id == request_id,
                project_pull_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")
    if request_row["stage"] != "awaiting-merge":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pull request must be approved before merge recording",
        )

    try:
        db.execute(
            update(project_pull_requests)
            .where(project_pull_requests.c.id == request_id)
            .values(
                stage="confirmation", merge_id=merge_id.strip(), merged_by_user_id=current_user_id
            )
        )
        db.execute(
            delete(project_pull_request_votes).where(
                project_pull_request_votes.c.request_id == request_id
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record merge"
        ) from exc

    return _governance_payload(db, project_row, current_user_id)
