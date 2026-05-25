from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import project_memberships, project_plan_votes, project_plans, projects
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66

ALLOWED_PLAN_TYPES_BY_MODE: dict[str, set[str]] = {
    "productive": {"production", "distribution"},
    "collective-service": {"organisation", "access"},
}

VALID_VOTES = {"yes", "no"}


def _serialize_plan(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "phase_kind": row["phase_kind"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "project_subtype": row["project_subtype"],
        "repository_url": row["repository_url"],
        "demand_consideration_note": row["demand_consideration_note"],
        "total_cost_label": row["total_cost_label"],
        "plan_payload": row["plan_payload"],
        "is_leading": row["is_leading"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "vote_summary": vote_summary,
    }


def _get_project_row_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(
        select(projects).where(projects.c.slug == slug.lower())
    ).mappings().first()
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can vote on plans")


def _assert_plan_type_allowed(project_mode: str, plan_type: str) -> None:
    allowed = ALLOWED_PLAN_TYPES_BY_MODE.get(project_mode)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This project mode does not support plan submissions",
        )
    if plan_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"plan_type must be one of: {sorted(allowed)}",
        )


def _compute_vote_summary(db: Session, plan_id: UUID, member_count: int) -> dict[str, object]:
    rows = db.execute(
        select(project_plan_votes.c.vote).where(project_plan_votes.c.plan_id == plan_id)
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
        "is_winning": meets_quorum and meets_approval,
    }


def submit_project_plan(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    plan_type: str,
    title: str,
    description: str,
    demand_consideration_note: str,
    total_cost_label: str | None,
    repository_url: str | None,
    plan_payload: dict[str, object],
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    normalized_type = plan_type.strip().lower()

    _assert_plan_type_allowed(project_row["project_mode"], normalized_type)
    _ensure_member(db, project_row["id"], current_user_id)

    try:
        created = db.execute(
            insert(project_plans)
            .values(
                project_id=project_row["id"],
                phase_kind=normalized_type,
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                project_subtype=project_row["project_subtype"],
                repository_url=repository_url.strip() if repository_url else None,
                demand_consideration_note=demand_consideration_note.strip(),
                total_cost_label=total_cost_label.strip() if total_cost_label else None,
                plan_payload=plan_payload,
                is_leading=False,
                status="open",
            )
            .returning(
                project_plans.c.id,
                project_plans.c.project_id,
                project_plans.c.phase_kind,
                project_plans.c.title,
                project_plans.c.description,
                project_plans.c.author_id,
                project_plans.c.project_subtype,
                project_plans.c.repository_url,
                project_plans.c.demand_consideration_note,
                project_plans.c.total_cost_label,
                project_plans.c.plan_payload,
                project_plans.c.is_leading,
                project_plans.c.status,
                project_plans.c.created_at,
                project_plans.c.updated_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not submit plan") from exc

    member_count = int(project_row["member_count"] or 0)
    summary = _compute_vote_summary(db, created["id"], member_count)
    return {"plan": _serialize_plan(created, summary)}


def list_project_plans(db: Session, project_slug: str, plan_type: str | None = None) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    member_count = int(project_row["member_count"] or 0)

    query = select(project_plans).where(project_plans.c.project_id == project_row["id"])
    if plan_type:
        normalized_type = plan_type.strip().lower()
        _assert_plan_type_allowed(project_row["project_mode"], normalized_type)
        query = query.where(project_plans.c.phase_kind == normalized_type)

    rows = db.execute(query.order_by(project_plans.c.created_at.desc())).mappings().all()

    items = []
    for row in rows:
        summary = _compute_vote_summary(db, row["id"], member_count)
        items.append(_serialize_plan(row, summary))

    return {
        "project_slug": project_row["slug"],
        "project_mode": project_row["project_mode"],
        "total": len(items),
        "items": items,
    }


def cast_project_plan_vote(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    plan_id: UUID,
    vote: str,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    plan_row = db.execute(
        select(project_plans).where(
            project_plans.c.id == plan_id,
            project_plans.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    existing_vote = db.execute(
        select(project_plan_votes.c.vote).where(
            project_plan_votes.c.plan_id == plan_id,
            project_plan_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_plan_votes).values(
                    plan_id=plan_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_plan_votes)
                .where(
                    project_plan_votes.c.plan_id == plan_id,
                    project_plan_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        member_count = int(project_row["member_count"] or 0)
        summary = _compute_vote_summary(db, plan_id, member_count)

        if summary["is_winning"]:
            db.execute(
                update(project_plans)
                .where(
                    project_plans.c.project_id == project_row["id"],
                    project_plans.c.phase_kind == plan_row["phase_kind"],
                )
                .values(is_leading=False)
            )
            db.execute(
                update(project_plans)
                .where(project_plans.c.id == plan_id)
                .values(is_leading=True, status="approved")
            )
            plan_is_leading = True
        else:
            db.execute(
                update(project_plans)
                .where(project_plans.c.id == plan_id)
                .values(is_leading=False)
            )
            plan_is_leading = False

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "project-plan", "target_id": str(plan_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast plan vote") from exc

    refreshed_plan = db.execute(
        select(project_plans).where(project_plans.c.id == plan_id)
    ).mappings().one()
    final_summary = _compute_vote_summary(db, plan_id, int(project_row["member_count"] or 0))

    if plan_is_leading and plan_row["author_id"] is not None:
        create_notification(
            db=db,
            recipient_id=plan_row["author_id"],
            actor_id=current_user_id,
            kind="prj-plan-lead",
            surface="project",
            subject_type="project-plan",
            subject_id=plan_id,
            target_id=project_row["id"],
            title="Plan became leading",
            body="A vote passed and your plan is now leading.",
            href=f"/projects/{project_row['slug']}",
        )

    return {
        "plan": _serialize_plan(refreshed_plan, final_summary),
        "vote": normalized_vote,
        "is_leading": plan_is_leading,
    }
