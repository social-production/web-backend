from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_merge_capability_change_requests,
    project_merge_capability_change_votes,
    project_merge_capability_members,
    project_plans,
    projects,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.projects.software.constants import VALID_ACTIONS, VALID_VOTES
from app.services.projects.software.governance import _compute_vote_summary, _governance_payload
from app.services.projects.software.helpers import (
    _ensure_member,
    _ensure_software_tables,
    _get_project_by_slug,
    _vote_rows,
)
from app.utils.votes import resolve_project_vote_population


def request_merge_capability_change(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_user_id: UUID,
    action: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)
    _ensure_member(db, project_row["id"], target_user_id)

    normalized_action = action.strip().lower()
    if normalized_action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action must be one of: ['grant', 'revoke']",
        )

    try:
        db.execute(
            insert(project_merge_capability_change_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                action=normalized_action,
                target_user_id=target_user_id,
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
            detail="Could not create merge capability request",
        ) from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_merge_capability_change(
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
            select(project_merge_capability_change_requests).where(
                project_merge_capability_change_requests.c.id == request_id,
                project_merge_capability_change_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Merge capability request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Request is already closed"
        )

    existing = (
        db.execute(
            select(project_merge_capability_change_votes).where(
                project_merge_capability_change_votes.c.request_id == request_id,
                project_merge_capability_change_votes.c.voter_id == current_user_id,
            )
        )
        .mappings()
        .first()
    )

    try:
        if existing is None:
            db.execute(
                insert(project_merge_capability_change_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_merge_capability_change_votes)
                .where(
                    project_merge_capability_change_votes.c.request_id == request_id,
                    project_merge_capability_change_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_merge_capability_change_votes, request_id),
            resolve_project_vote_population(
                db, project_row["id"], bool(project_row.get("is_platform_tagged"))
            ),
            current_user_id,
        )

        if passes:
            if request_row["action"] == "grant":
                db.execute(
                    pg_insert(project_merge_capability_members)
                    .values(
                        project_id=project_row["id"],
                        user_id=request_row["target_user_id"],
                        source_label="approved-request",
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
                )
            else:
                db.execute(
                    delete(project_merge_capability_members).where(
                        project_merge_capability_members.c.project_id == project_row["id"],
                        project_merge_capability_members.c.user_id == request_row["target_user_id"],
                    )
                )
            db.execute(
                update(project_merge_capability_change_requests)
                .where(project_merge_capability_change_requests.c.id == request_id)
                .values(status="approved")
            )
        elif not can_still_pass:
            db.execute(
                update(project_merge_capability_change_requests)
                .where(project_merge_capability_change_requests.c.id == request_id)
                .values(status="rejected")
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "merge-capability-request",
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


def _set_merge_capability_members(
    db: Session,
    project_id: UUID,
    user_ids: list[UUID],
    source_label: str,
) -> None:
    _ensure_software_tables(db)
    db.execute(
        delete(project_merge_capability_members).where(
            project_merge_capability_members.c.project_id == project_id,
            project_merge_capability_members.c.source_label == source_label,
        )
    )
    for user_id in user_ids:
        db.execute(
            pg_insert(project_merge_capability_members)
            .values(project_id=project_id, user_id=user_id, source_label=source_label)
            .on_conflict_do_update(
                index_elements=["project_id", "user_id"],
                set_={"source_label": source_label},
            )
        )


def sync_merge_capability_for_leading_plan(db: Session, project_id: UUID, plan_id: UUID) -> None:
    _ensure_software_tables(db)
    project_row = db.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
    if project_row is None or project_row["project_subtype"] != "software":
        return

    plan_row = (
        db.execute(
            select(project_plans.c.author_id).where(
                project_plans.c.id == plan_id,
                project_plans.c.project_id == project_id,
                project_plans.c.is_leading.is_(True),
            )
        )
        .mappings()
        .first()
    )
    if plan_row is None:
        return

    if bool(project_row.get("is_platform_tagged")):
        sync_platform_software_merge_capability(db, project_id=project_id)
        return

    if plan_row["author_id"] is not None:
        _set_merge_capability_members(db, project_id, [plan_row["author_id"]], "plan-creator")


def sync_platform_software_merge_capability(db: Session, project_id: UUID | None = None) -> None:
    from app.services.board import get_active_board_member_ids

    _ensure_software_tables(db)
    board_member_ids = get_active_board_member_ids(db)

    query = select(projects.c.id).where(
        projects.c.project_subtype == "software",
        projects.c.is_platform_tagged.is_(True),
    )
    if project_id is not None:
        query = query.where(projects.c.id == project_id)

    project_ids = [row[0] for row in db.execute(query).all()]
    for pid in project_ids:
        db.execute(
            delete(project_merge_capability_members).where(
                project_merge_capability_members.c.project_id == pid,
                project_merge_capability_members.c.source_label == "platform-board",
            )
        )
        if board_member_ids:
            _set_merge_capability_members(db, pid, board_member_ids, "platform-board")
