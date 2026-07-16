from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_plan_criterion_ratings,
    project_plan_value_votes,
    project_plan_votes,
    project_plans,
    project_values,
    projects,
)
from app.services.governance_votes import compute_plan_vote_summary
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.plan_criteria import (
    VALID_PLAN_RATINGS,
    assessment_criteria_for_plan,
    parse_value_criterion_id,
)
from app.utils.votes import resolve_project_vote_population

APPROVAL_THRESHOLD = 0.66

ALLOWED_PLAN_TYPES_BY_MODE: dict[str, set[str]] = {
    "productive": {"production", "distribution"},
    "collective-service": {"organisation", "access"},
}

VALID_VOTES = {"yes", "no", "neutral"}
VALID_PROJECT_SUBTYPES = {"standard", "software", "asset-management"}


def _plan_subtype_from_payload(
    plan_payload: Mapping[str, object] | None,
    fallback: str | None = None,
) -> str | None:
    if plan_payload is None:
        return fallback

    raw = plan_payload.get("projectSubtype")
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in VALID_PROJECT_SUBTYPES:
            return normalized

    return fallback


def _subtype_label(subtype: str | None) -> str:
    if subtype == "software":
        return "Software"
    if subtype == "asset-management":
        return "Asset management"
    return "Standard"


def _serialize_plan(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
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
            status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can vote on plans"
        )


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


def sync_project_plan_leading_flags(
    db: Session,
    project_id: UUID,
    phase_kind: str,
    member_count: int,
) -> UUID | None:
    plan_ids = (
        db.execute(
            select(project_plans.c.id).where(
                project_plans.c.project_id == project_id,
                project_plans.c.phase_kind == phase_kind,
            )
        )
        .scalars()
        .all()
    )

    candidates: list[tuple[UUID, float]] = []
    for plan_id in plan_ids:
        summary = _compute_vote_summary(db, plan_id, member_count)
        if summary["is_winning"]:
            candidates.append((plan_id, float(summary["approval_ratio"])))

    db.execute(
        update(project_plans)
        .where(
            project_plans.c.project_id == project_id,
            project_plans.c.phase_kind == phase_kind,
        )
        .values(is_leading=False)
    )

    leader_id: UUID | None = None
    if candidates:
        max_ratio = max(ratio for _, ratio in candidates)
        top = [plan_id for plan_id, ratio in candidates if ratio == max_ratio]
        if len(top) == 1:
            leader_id = top[0]
            db.execute(
                update(project_plans)
                .where(project_plans.c.id == leader_id)
                .values(is_leading=True, status="approved")
            )

    if leader_id is not None:
        leader_row = (
            db.execute(
                select(project_plans.c.project_subtype, project_plans.c.plan_payload).where(
                    project_plans.c.id == leader_id
                )
            )
            .mappings()
            .first()
        )
        if leader_row is not None and phase_kind in {"production", "organisation"}:
            resolved_subtype = leader_row["project_subtype"] or _plan_subtype_from_payload(
                dict(leader_row["plan_payload"] or {})
            )
            if resolved_subtype:
                db.execute(
                    update(projects)
                    .where(projects.c.id == project_id)
                    .values(project_subtype=resolved_subtype)
                )

        from app.services.projects_software import sync_merge_capability_for_leading_plan

        sync_merge_capability_for_leading_plan(db, project_id, leader_id)

    return leader_id


def _compute_vote_summary(db: Session, plan_id: UUID, member_count: int) -> dict[str, object]:
    return compute_plan_vote_summary(db, project_plan_votes, plan_id, member_count)


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
    plan_subtype = _plan_subtype_from_payload(plan_payload, project_row["project_subtype"])

    try:
        created = (
            db.execute(
                insert(project_plans)
                .values(
                    project_id=project_row["id"],
                    phase_kind=normalized_type,
                    title=title.strip(),
                    description=description.strip(),
                    author_id=current_user_id,
                    project_subtype=plan_subtype,
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
            )
            .mappings()
            .one()
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="submit-project-plan",
            metadata={"project_slug": project_slug, "plan_id": str(created["id"])},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not submit plan"
        ) from exc

    vote_context_population = resolve_project_vote_population(
        db,
        project_row["id"],
        bool(project_row["is_platform_tagged"]),
    )
    summary = _compute_vote_summary(db, created["id"], vote_context_population)
    return {"plan": _serialize_plan(created, summary)}


def list_project_plans(
    db: Session, project_slug: str, plan_type: str | None = None
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    vote_context_population = resolve_project_vote_population(
        db,
        project_row["id"],
        bool(project_row["is_platform_tagged"]),
    )

    query = select(project_plans).where(project_plans.c.project_id == project_row["id"])
    if plan_type:
        normalized_type = plan_type.strip().lower()
        _assert_plan_type_allowed(project_row["project_mode"], normalized_type)
        query = query.where(project_plans.c.phase_kind == normalized_type)

    rows = db.execute(query.order_by(project_plans.c.created_at.desc())).mappings().all()

    items = []
    for row in rows:
        summary = _compute_vote_summary(db, row["id"], vote_context_population)
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

    plan_row = (
        db.execute(
            select(project_plans).where(
                project_plans.c.id == plan_id,
                project_plans.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    existing_vote = db.execute(
        select(project_plan_votes.c.vote).where(
            project_plan_votes.c.plan_id == plan_id,
            project_plan_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if normalized_vote == "neutral":
            if existing_vote is not None:
                db.execute(
                    delete(project_plan_votes).where(
                        project_plan_votes.c.plan_id == plan_id,
                        project_plan_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing_vote is None:
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

        vote_context_population = resolve_project_vote_population(
            db,
            project_row["id"],
            bool(project_row["is_platform_tagged"]),
        )
        previous_leader = db.execute(
            select(project_plans.c.id).where(
                project_plans.c.project_id == project_row["id"],
                project_plans.c.phase_kind == plan_row["phase_kind"],
                project_plans.c.is_leading.is_(True),
            )
        ).scalar()
        new_leader = sync_project_plan_leading_flags(
            db,
            project_row["id"],
            plan_row["phase_kind"],
            vote_context_population,
        )
        plan_is_leading = new_leader == plan_id
        leader_changed = new_leader is not None and new_leader != previous_leader

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "project-plan",
                "target_id": str(plan_id),
                "vote": normalized_vote,
            },
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast plan vote"
        ) from exc

    refreshed_plan = (
        db.execute(select(project_plans).where(project_plans.c.id == plan_id)).mappings().one()
    )
    final_summary = _compute_vote_summary(
        db,
        plan_id,
        resolve_project_vote_population(
            db,
            project_row["id"],
            bool(project_row["is_platform_tagged"]),
        ),
    )

    if leader_changed and new_leader is not None:
        leader_author_id = db.execute(
            select(project_plans.c.author_id).where(project_plans.c.id == new_leader)
        ).scalar()
        if leader_author_id is not None:
            create_notification(
                db=db,
                recipient_id=leader_author_id,
                actor_id=current_user_id,
                kind="prj-plan-lead",
                surface="project",
                subject_type="project-plan",
                subject_id=new_leader,
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


def cast_project_plan_value_vote(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    plan_id: UUID,
    value_id: UUID,
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
        select(project_plans.c.id).where(
            project_plans.c.id == plan_id,
            project_plans.c.project_id == project_row["id"],
        )
    ).first()
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    value_row = db.execute(
        select(project_values.c.id).where(
            project_values.c.id == value_id,
            project_values.c.project_id == project_row["id"],
        )
    ).first()
    if value_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project value not found")

    existing_vote = db.execute(
        select(project_plan_value_votes.c.vote).where(
            project_plan_value_votes.c.plan_id == plan_id,
            project_plan_value_votes.c.value_id == value_id,
            project_plan_value_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if normalized_vote == "neutral":
            if existing_vote is not None:
                db.execute(
                    delete(project_plan_value_votes).where(
                        project_plan_value_votes.c.plan_id == plan_id,
                        project_plan_value_votes.c.value_id == value_id,
                        project_plan_value_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing_vote is None:
            db.execute(
                insert(project_plan_value_votes).values(
                    plan_id=plan_id,
                    value_id=value_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_plan_value_votes)
                .where(
                    project_plan_value_votes.c.plan_id == plan_id,
                    project_plan_value_votes.c.value_id == value_id,
                    project_plan_value_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "project-plan-value",
                "target_id": str(plan_id),
                "value_id": str(value_id),
                "vote": normalized_vote,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not cast plan value vote",
        ) from exc

    return {
        "ok": True,
        "project_slug": project_row["slug"],
        "plan_id": plan_id,
        "value_id": value_id,
        "vote": normalized_vote,
    }


def cast_project_plan_criterion_rating(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    plan_id: UUID,
    criterion_id: str,
    rating: int | None,
) -> dict[str, object]:
    project_row = _get_project_row_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    plan_row = (
        db.execute(
            select(project_plans).where(
                project_plans.c.id == plan_id,
                project_plans.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    value_rows = db.execute(
        select(project_values.c.id, project_values.c.label).where(
            project_values.c.project_id == project_row["id"]
        )
    ).all()
    prominent_values = [(row[0], row[1]) for row in value_rows]
    allowed_criteria = {
        item["criterionId"]
        for item in assessment_criteria_for_plan(
            plan_kind=str(plan_row["phase_kind"]),
            prominent_values=prominent_values,
            project_subtype=plan_row["project_subtype"],
        )
    }
    normalized_criterion_id = criterion_id.strip()
    if normalized_criterion_id not in allowed_criteria:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown plan criterion"
        )

    value_id = parse_value_criterion_id(normalized_criterion_id)
    if value_id is not None:
        value_row = db.execute(
            select(project_values.c.id).where(
                project_values.c.id == value_id,
                project_values.c.project_id == project_row["id"],
            )
        ).first()
        if value_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project value not found"
            )

    existing_rating = db.execute(
        select(project_plan_criterion_ratings.c.rating).where(
            project_plan_criterion_ratings.c.plan_id == plan_id,
            project_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
            project_plan_criterion_ratings.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if rating is None:
            if existing_rating is not None:
                db.execute(
                    delete(project_plan_criterion_ratings).where(
                        project_plan_criterion_ratings.c.plan_id == plan_id,
                        project_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
                        project_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                )
            normalized_rating = None
        else:
            if rating not in VALID_PLAN_RATINGS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="rating must be between 1 and 5",
                )
            if existing_rating is None:
                db.execute(
                    insert(project_plan_criterion_ratings).values(
                        plan_id=plan_id,
                        criterion_id=normalized_criterion_id,
                        voter_id=current_user_id,
                        rating=rating,
                    )
                )
            else:
                db.execute(
                    update(project_plan_criterion_ratings)
                    .where(
                        project_plan_criterion_ratings.c.plan_id == plan_id,
                        project_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
                        project_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                    .values(rating=rating)
                )
            normalized_rating = rating

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "project-plan-criterion",
                "target_id": str(plan_id),
                "criterion_id": normalized_criterion_id,
                "rating": normalized_rating,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not cast plan criterion rating",
        ) from exc

    return {
        "ok": True,
        "project_slug": project_row["slug"],
        "plan_id": plan_id,
        "criterion_id": normalized_criterion_id,
        "rating": normalized_rating,
    }
