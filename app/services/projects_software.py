from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import governance_decision_history, project_memberships, project_plans, projects
from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66
VALID_VOTES = frozenset({"yes", "no"})

DECISION_KIND_PULL_REQUEST = "software_pull_request"
DECISION_KIND_MERGE_CAPABILITY = "software_merge_capability_change"
DECISION_KIND_REPOSITORY_REPLACEMENT = "software_repository_replacement"
DECISION_KINDS = frozenset(
    {
        DECISION_KIND_PULL_REQUEST,
        DECISION_KIND_MERGE_CAPABILITY,
        DECISION_KIND_REPOSITORY_REPLACEMENT,
    }
)


def _serialize_vote_summary(votes: dict[str, str], member_count: int) -> dict[str, object]:
    yes_count = sum(1 for vote in votes.values() if vote == "yes")
    no_count = sum(1 for vote in votes.values() if vote == "no")
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


def _serialize_decision(row: Mapping[str, object], member_count: int) -> dict[str, object]:
    payload = dict(row["payload"] or {})
    votes = dict(payload.get("votes") or {})
    return {
        "id": row["id"],
        "decision_kind": row["decision_kind"],
        "status": row["status"],
        "author_id": row["author_id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "payload": payload,
        "vote_summary": _serialize_vote_summary(votes, member_count),
    }


def _get_project_row_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


def _ensure_software_project(project_row: Mapping[str, object]) -> None:
    if project_row["project_subtype"] != "software":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Software features require a software project",
        )


def _membership_row(db: Session, project_id: UUID, user_id: UUID) -> Mapping[str, object] | None:
    return db.execute(
        select(project_memberships).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).mappings().first()


def _ensure_member(db: Session, project_id: UUID, user_id: UUID) -> Mapping[str, object]:
    row = _membership_row(db, project_id, user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can perform this action")
    return row


def _ensure_merge_capability(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = _ensure_member(db, project_id, user_id)
    if not bool(membership["is_manager"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only members with merge capability can record merges",
        )


def _member_count(project_row: Mapping[str, object]) -> int:
    return int(project_row.get("member_count") or 0)


def _create_decision(
    db: Session,
    project_row: Mapping[str, object],
    current_user_id: UUID,
    decision_kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    try:
        row = db.execute(
            insert(governance_decision_history)
            .values(
                entity_kind="project",
                entity_id=project_row["id"],
                decision_kind=decision_kind,
                status="open",
                approval_threshold_percent=Decimal("66.00"),
                payload={**payload, "votes": {}},
                author_id=current_user_id,
                resolved_at=None,
            )
            .returning(
                governance_decision_history.c.id,
                governance_decision_history.c.decision_kind,
                governance_decision_history.c.status,
                governance_decision_history.c.author_id,
                governance_decision_history.c.payload,
                governance_decision_history.c.created_at,
                governance_decision_history.c.resolved_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create request") from exc

    return {"request": _serialize_decision(row, _member_count(project_row))}


def _load_decision(
    db: Session,
    project_id: UUID,
    decision_id: UUID,
    expected_kind: str,
) -> Mapping[str, object]:
    row = db.execute(
        select(governance_decision_history).where(
            governance_decision_history.c.id == decision_id,
            governance_decision_history.c.entity_kind == "project",
            governance_decision_history.c.entity_id == project_id,
            governance_decision_history.c.decision_kind == expected_kind,
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return row


def _finalize_if_passing(
    db: Session,
    project_row: Mapping[str, object],
    row: Mapping[str, object],
    payload: dict[str, object],
) -> tuple[bool, Mapping[str, object]]:
    votes = dict(payload.get("votes") or {})
    summary = _serialize_vote_summary(votes, _member_count(project_row))
    executed = False

    if row["status"] == "open" and summary["is_passing"]:
        decision_kind = row["decision_kind"]
        if decision_kind == DECISION_KIND_MERGE_CAPABILITY:
            target_user_id = UUID(str(payload["target_user_id"]))
            enable = bool(payload.get("enable_merge", True))
            db.execute(
                update(project_memberships)
                .where(
                    project_memberships.c.project_id == project_row["id"],
                    project_memberships.c.user_id == target_user_id,
                )
                .values(is_manager=enable)
            )
        elif decision_kind == DECISION_KIND_REPOSITORY_REPLACEMENT:
            new_repository_url = str(payload["new_repository_url"])
            db.execute(
                update(project_plans)
                .where(
                    project_plans.c.project_id == project_row["id"],
                    project_plans.c.is_leading.is_(True),
                )
                .values(repository_url=new_repository_url)
            )
        elif decision_kind == DECISION_KIND_PULL_REQUEST:
            pass

        db.execute(
            update(governance_decision_history)
            .where(governance_decision_history.c.id == row["id"])
            .values(status="approved", resolved_at=datetime.now(timezone.utc))
        )
        executed = True

    refreshed = db.execute(
        select(governance_decision_history).where(governance_decision_history.c.id == row["id"])
    ).mappings().one()
    return executed, refreshed


def submit_pull_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    description: str,
    pull_request_url: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    _ensure_member(db, project_row["id"], current_user_id)

    return _create_decision(
        db,
        project_row,
        current_user_id,
        DECISION_KIND_PULL_REQUEST,
        {
            "title": title.strip(),
            "description": description.strip(),
            "pull_request_url": pull_request_url.strip(),
            "merged": False,
            "merged_by": None,
            "merge_commit_id": None,
        },
    )


def request_merge_capability_change(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_user_id: UUID,
    enable_merge: bool,
    reason: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    _ensure_member(db, project_row["id"], current_user_id)
    _ensure_member(db, project_row["id"], target_user_id)

    return _create_decision(
        db,
        project_row,
        current_user_id,
        DECISION_KIND_MERGE_CAPABILITY,
        {
            "target_user_id": str(target_user_id),
            "enable_merge": enable_merge,
            "reason": reason.strip(),
        },
    )


def request_repository_replacement(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    new_repository_url: str,
    reason: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    _ensure_member(db, project_row["id"], current_user_id)

    current_repo = db.execute(
        select(project_plans.c.repository_url)
        .where(
            project_plans.c.project_id == project_row["id"],
            project_plans.c.is_leading.is_(True),
        )
        .limit(1)
    ).scalar_one_or_none()

    return _create_decision(
        db,
        project_row,
        current_user_id,
        DECISION_KIND_REPOSITORY_REPLACEMENT,
        {
            "current_repository_url": current_repo,
            "new_repository_url": new_repository_url.strip(),
            "reason": reason.strip(),
        },
    )


def list_software_requests(db: Session, project_slug: str, kind: str) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    if kind not in DECISION_KINDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported request kind")

    rows = db.execute(
        select(governance_decision_history)
        .where(
            governance_decision_history.c.entity_kind == "project",
            governance_decision_history.c.entity_id == project_row["id"],
            governance_decision_history.c.decision_kind == kind,
        )
        .order_by(governance_decision_history.c.created_at.desc())
    ).mappings().all()

    items = [_serialize_decision(row, _member_count(project_row)) for row in rows]
    return {"project_slug": project_row["slug"], "total": len(items), "items": items}


def vote_software_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    decision_id: UUID,
    kind: str,
    vote: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    _ensure_member(db, project_row["id"], current_user_id)
    if kind not in DECISION_KINDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported request kind")

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be one of: ['no', 'yes']")

    row = _load_decision(db, project_row["id"], decision_id, kind)
    if row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request is already closed")

    payload = dict(row["payload"] or {})
    votes = dict(payload.get("votes") or {})
    votes[str(current_user_id)] = normalized_vote
    payload["votes"] = votes

    try:
        db.execute(
            update(governance_decision_history)
            .where(governance_decision_history.c.id == decision_id)
            .values(payload=payload)
        )
        executed, refreshed = _finalize_if_passing(db, project_row, row, payload)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record vote") from exc

    return {
        "request": _serialize_decision(refreshed, _member_count(project_row)),
        "vote": normalized_vote,
        "executed": executed,
    }


def record_pull_request_merge(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    decision_id: UUID,
    merge_commit_id: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_software_project(project_row)
    _ensure_merge_capability(db, project_row["id"], current_user_id)

    row = _load_decision(db, project_row["id"], decision_id, DECISION_KIND_PULL_REQUEST)
    if row["status"] != "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pull request must be approved before merge recording")

    payload = dict(row["payload"] or {})
    if bool(payload.get("merged")):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pull request merge already recorded")

    payload["merged"] = True
    payload["merged_by"] = str(current_user_id)
    payload["merge_commit_id"] = merge_commit_id.strip()

    try:
        db.execute(
            update(governance_decision_history)
            .where(governance_decision_history.c.id == decision_id)
            .values(payload=payload, status="merged", resolved_at=datetime.now(timezone.utc))
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record merge") from exc

    refreshed = db.execute(
        select(governance_decision_history).where(governance_decision_history.c.id == decision_id)
    ).mappings().one()

    return {
        "request": _serialize_decision(refreshed, _member_count(project_row)),
        "merged": True,
        "merge_commit_id": payload["merge_commit_id"],
    }
