from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_merge_capability_members,
    project_plans,
    project_pull_requests,
    project_repository_replacement_requests,
    project_repository_replacement_votes,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.projects.software.constants import VALID_VOTES
from app.services.projects.software.governance import _compute_vote_summary, _governance_payload
from app.services.projects.software.helpers import (
    _ensure_member,
    _ensure_software_tables,
    _get_project_by_slug,
    _vote_rows,
)
from app.utils.votes import resolve_project_vote_population


def request_repository_replacement(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    repository_url: str,
    reason: str,
    related_pull_request_id: UUID,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    related = (
        db.execute(
            select(project_pull_requests).where(
                project_pull_requests.c.id == related_pull_request_id,
                project_pull_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if related is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Related pull request not found"
        )

    leading_plan = (
        db.execute(
            select(project_plans.c.repository_url)
            .where(
                project_plans.c.project_id == project_row["id"],
                project_plans.c.is_leading.is_(True),
            )
            .limit(1)
        )
        .mappings()
        .first()
    )
    previous_repository_url = str((leading_plan or {}).get("repository_url") or "")

    try:
        db.execute(
            insert(project_repository_replacement_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                repository_url=repository_url.strip(),
                previous_repository_url=previous_repository_url,
                reason=reason.strip(),
                related_pull_request_id=related_pull_request_id,
                author_id=current_user_id,
                status="open",
                approval_threshold_percent=Decimal("66.00"),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create repository replacement request",
        ) from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_repository_replacement(
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
            select(project_repository_replacement_requests).where(
                project_repository_replacement_requests.c.id == request_id,
                project_repository_replacement_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Repository replacement request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Request is already closed"
        )

    existing = (
        db.execute(
            select(project_repository_replacement_votes).where(
                project_repository_replacement_votes.c.request_id == request_id,
                project_repository_replacement_votes.c.voter_id == current_user_id,
            )
        )
        .mappings()
        .first()
    )

    try:
        if existing is None:
            db.execute(
                insert(project_repository_replacement_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_repository_replacement_votes)
                .where(
                    project_repository_replacement_votes.c.request_id == request_id,
                    project_repository_replacement_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_repository_replacement_votes, request_id),
            resolve_project_vote_population(
                db, project_row["id"], bool(project_row.get("is_platform_tagged"))
            ),
            current_user_id,
        )

        if passes:
            db.execute(
                update(project_repository_replacement_requests)
                .where(project_repository_replacement_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(project_plans)
                .where(
                    project_plans.c.project_id == project_row["id"],
                    project_plans.c.is_leading.is_(True),
                )
                .values(repository_url=request_row["repository_url"])
            )
            db.execute(
                update(project_pull_requests)
                .where(project_pull_requests.c.id == request_row["related_pull_request_id"])
                .values(stage="replaced")
            )
            if request_row["author_id"] is not None:
                db.execute(
                    delete(project_merge_capability_members).where(
                        project_merge_capability_members.c.project_id == project_row["id"],
                        project_merge_capability_members.c.source_label.in_(
                            ["plan-creator", "repo-replacement"]
                        ),
                    )
                )
                db.execute(
                    pg_insert(project_merge_capability_members)
                    .values(
                        project_id=project_row["id"],
                        user_id=request_row["author_id"],
                        source_label="repo-replacement",
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
                )
        elif not can_still_pass:
            db.execute(
                update(project_repository_replacement_requests)
                .where(project_repository_replacement_requests.c.id == request_id)
                .values(status="rejected")
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "repository-replacement-request",
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

    return _governance_payload(db, project_row, current_user_id)
