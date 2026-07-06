from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plans,
    project_update_request_votes,
    project_update_requests,
    project_updates,
    projects,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document
from app.utils.votes import required_votes, resolve_project_vote_population

APPROVAL_THRESHOLD = 0.66
VALID_PHASE_IDS = frozenset({"phase-1", "phase-2", "phase-3", "phase-4", "phase-5", "phase-6", "phase-7"})
VALID_VOTES = frozenset({"yes", "no"})
STAGE_LABEL_BY_PHASE_ID = {
    "phase-1": "Proposal",
    "phase-2": "Production Plan",
    "phase-3": "Distribution Plan",
    "phase-4": "Acquisition",
    "phase-5": "Activity",
    "phase-6": "Pending Execution",
    "phase-7": "Closed",
}

PHASE_ORDER = {phase_id: index for index, phase_id in enumerate(sorted(VALID_PHASE_IDS), start=1)}


def effective_phase_id_for_progress(phase_id: str) -> str:
    if phase_id == "phase-4":
        return "phase-3"
    if phase_id == "phase-6":
        return "phase-5"
    return phase_id


def display_stage_label(project_mode: str, project_subtype: str | None, phase_id: str) -> str:
    if project_mode == "personal-service":
        if phase_id == "phase-1":
            return "Activity"
        if phase_id == "phase-2":
            return "Closed"
        return "Activity"

    normalized_phase_id = effective_phase_id_for_progress(phase_id)
    return STAGE_LABEL_BY_PHASE_ID.get(normalized_phase_id, "Proposal")


def lifecycle_phase_title(project_mode: str, phase_id: str, default_title: str) -> str:
    if project_mode == "personal-service":
        if phase_id == "phase-1":
            return "Activity"
        if phase_id == "phase-2":
            return "Closed"
    if project_mode == "collective-service":
        if phase_id == "phase-2":
            return "Operations Plan"
        if phase_id == "phase-3":
            return "Access Plan"
    return default_title


def _skips_distribution_phase(project_mode: str, project_subtype: str | None) -> bool:
    return project_mode == "collective-service" or project_subtype == "software"


def visible_phase_ids_for_project(
    project_mode: str,
    project_subtype: str | None,
    current_phase_id: str,
) -> list[str]:
    if project_mode == "personal-service":
        return ["phase-1", "phase-2"]

    if _skips_distribution_phase(project_mode, project_subtype):
        return ["phase-1", "phase-2", "phase-5", "phase-7"]

    return ["phase-1", "phase-2", "phase-3", "phase-5", "phase-7"]


def next_phase_id_for_project(
    project_mode: str,
    project_subtype: str | None,
    current_phase_id: str,
) -> str | None:
    if project_mode == "personal-service":
        if current_phase_id == "phase-1":
            return "phase-2"
        return None

    if current_phase_id == "phase-6":
        return "phase-7"

    current_order = PHASE_ORDER.get(current_phase_id)
    if current_order is None:
        return None

    next_order = current_order + 1
    while next_order <= PHASE_ORDER["phase-7"]:
        next_phase_id = next((phase_id for phase_id, order in PHASE_ORDER.items() if order == next_order), None)
        if next_phase_id is None:
            return None

        if next_phase_id == "phase-3" and _skips_distribution_phase(project_mode, project_subtype):
            next_order += 1
            continue

        if next_phase_id == "phase-4":
            next_order += 1
            continue

        if next_phase_id == "phase-6":
            next_order += 1
            continue

        return next_phase_id

    return None


def _required_leading_plan_kind(
    project_mode: str,
    project_subtype: str | None,
    current_phase_id: str,
) -> str | None:
    if current_phase_id == "phase-2":
        return "organisation" if project_mode == "collective-service" else "production"
    if current_phase_id == "phase-3":
        if _skips_distribution_phase(project_mode, project_subtype):
            return None
        return "access" if project_mode == "collective-service" else "distribution"
    return None


def _ensure_project_phase_plan_gate(db: Session, project_row: Mapping[str, object], target_phase_id: str) -> None:
    current_phase_id = str(project_row["current_phase_id"])
    current_order = PHASE_ORDER.get(current_phase_id)
    target_order = PHASE_ORDER.get(target_phase_id)
    if current_order is None or target_order is None or target_order <= current_order:
        return

    project_subtype = str(project_row["project_subtype"]) if project_row.get("project_subtype") else None
    required_kind = _required_leading_plan_kind(
        str(project_row["project_mode"]),
        project_subtype,
        current_phase_id,
    )
    if required_kind is None:
        return

    leading_plan = db.execute(
        select(project_plans.c.id).where(
            project_plans.c.project_id == project_row["id"],
            project_plans.c.phase_kind == required_kind,
            project_plans.c.is_leading.is_(True),
            project_plans.c.status == "approved",
        )
    ).first()
    if leading_plan is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An approved plan is required before advancing this project phase",
        )


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


def _project_vote_population(db: Session, project_row: Mapping[str, object]) -> int:
    return resolve_project_vote_population(
        db,
        project_row["id"],
        bool(project_row["is_platform_tagged"]),
    )


def _compute_simple_vote_summary(
    db: Session,
    vote_table,
    request_id: UUID,
    member_count: int,
) -> dict[str, object]:
    rows = db.execute(select(vote_table.c.vote).where(vote_table.c.request_id == request_id)).all()

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
    is_passing = meets_quorum and meets_approval

    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_meet_quorum = max_total >= votes_required
    can_meet_approval = (max_yes / max_total * 100.0) >= (APPROVAL_THRESHOLD * 100.0) if max_total > 0 else False
    can_still_pass = (not is_passing) and can_meet_quorum and can_meet_approval

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
        "is_passing": is_passing,
        "can_still_pass": can_still_pass,
    }


def _serialize_update_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "body": row["body"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _serialize_edit_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


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


def _ensure_governance_requests_allowed(project_mode: str) -> None:
    if project_mode == "personal-service":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="personal-service projects do not allow governance vote requests",
        )


def _ensure_manager(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(project_memberships.c.is_manager).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None or not bool(membership[0]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project managers can perform this action")


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
    is_passing = meets_quorum and meets_approval

    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_meet_quorum = max_total >= votes_required
    can_meet_approval = (max_yes / max_total * 100.0) >= (APPROVAL_THRESHOLD * 100.0) if max_total > 0 else False
    can_still_pass = (not is_passing) and can_meet_quorum and can_meet_approval

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
        "is_passing": is_passing,
        "can_still_pass": can_still_pass,
    }


def _phase_change_kind_for_project(target_phase_id: str, current_phase_id: str) -> str:
    if target_phase_id == "phase-7":
        return "close"
    target_order = PHASE_ORDER.get(target_phase_id, 0)
    current_order = PHASE_ORDER.get(current_phase_id, 0)
    if target_order > 0 and current_order > 0 and target_order < current_order:
        return "return"
    return "advance"


def create_phase_change_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_phase_id: str,
    reason: str,
    close_outcome: str | None = None,
    conversion_target_mode: str | None = None,
    conversion_target_subtype: str | None = None,
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

    # Phase 1 has no asset holding — acquisition (phase-4) is not available yet
    if normalized_target == "phase-4":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Acquisition phase is not available in Phase 1",
        )

    current_phase_id = project_row["current_phase_id"]
    if normalized_target == current_phase_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_phase_id must differ from current_phase_id",
        )

    _ensure_project_phase_plan_gate(db, project_row, normalized_target)

    change_kind = _phase_change_kind_for_project(normalized_target, current_phase_id)

    open_request = db.execute(
        select(project_phase_change_requests.c.id).where(
            project_phase_change_requests.c.project_id == project_row["id"],
            project_phase_change_requests.c.status == "open",
            project_phase_change_requests.c.target_phase_id == normalized_target,
        )
    ).first()
    if open_request:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A vote is already open — approve or reject it first.",
        )

    try:
        created = db.execute(
            insert(project_phase_change_requests)
            .values(
                project_id=project_row["id"],
                from_phase_id=current_phase_id,
                target_phase_id=normalized_target,
                change_kind=change_kind,
                close_outcome=close_outcome,
                conversion_target_mode=conversion_target_mode,
                conversion_target_subtype=conversion_target_subtype,
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

    summary = _compute_vote_summary(db, created["id"], _project_vote_population(db, project_row))
    member_ids = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_row["id"],
        )
    ).scalars().all()
    target_label = display_stage_label(
        str(project_row["project_mode"]),
        str(project_row["project_subtype"]) if project_row.get("project_subtype") else None,
        normalized_target,
    )
    for member_id in member_ids:
        if member_id == current_user_id:
            continue
        create_notification(
            db=db,
            recipient_id=member_id,
            actor_id=current_user_id,
            kind="prj-phase-vote",
            surface="project",
            subject_type="phase-change",
            subject_id=created["id"],
            target_id=project_row["id"],
            title="Project phase vote open",
            body=f"Vote on advancing to {target_label}.",
            href=f"/projects/{project_row['slug']}?open=vote&voteKind=phase_change&voteTarget={created['id']}",
        )
    db.commit()
    return {"request": _serialize_phase_request(created, summary)}


def list_phase_change_requests(db: Session, project_slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_phase_requests_allowed(project_row["project_mode"])

    member_count = _project_vote_population(db, project_row)
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

        summary = _compute_vote_summary(db, request_id, _project_vote_population(db, project_row))

        executed = False
        if summary["is_passing"]:
            target_phase_id = request_row["target_phase_id"]
            db.execute(
                update(project_phase_change_requests)
                .where(project_phase_change_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(project_phase_change_requests)
                .where(
                    project_phase_change_requests.c.project_id == project_row["id"],
                    project_phase_change_requests.c.id != request_id,
                    project_phase_change_requests.c.status == "open",
                )
                .values(status="closed")
            )
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(
                    current_phase_id=target_phase_id,
                    stage_label=display_stage_label(
                        str(project_row["project_mode"]),
                        str(project_row["project_subtype"]) if project_row["project_subtype"] else None,
                        target_phase_id,
                    ),
                )
            )
            if target_phase_id == "phase-7":
                close_note = (request_row["reason"] or "").strip()
                if close_note:
                    db.execute(
                        insert(project_updates).values(
                            project_id=project_row["id"],
                            title="Closure note",
                            body=close_note,
                            author_id=request_row["author_id"] or current_user_id,
                        )
                    )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(project_phase_change_requests)
                .where(project_phase_change_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={"target_type": "project-phase-change", "target_id": str(request_id), "vote": normalized_vote},
    )

    refreshed_request = db.execute(
        select(project_phase_change_requests).where(project_phase_change_requests.c.id == request_id)
    ).mappings().one()
    refreshed_project = db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    final_summary = _compute_vote_summary(db, request_id, _project_vote_population(db, refreshed_project))

    if executed:
        member_ids = db.execute(
            select(project_memberships.c.user_id).where(
                project_memberships.c.project_id == project_row["id"],
            )
        ).scalars().all()
        target_label = display_stage_label(
            str(refreshed_project["project_mode"]),
            str(refreshed_project["project_subtype"]) if refreshed_project.get("project_subtype") else None,
            str(refreshed_project["current_phase_id"]),
        )
        for member_id in member_ids:
            if member_id == current_user_id:
                continue
            create_notification(
                db=db,
                recipient_id=member_id,
                actor_id=current_user_id,
                kind="prj-phase-done",
                surface="project",
                subject_type="phase-change",
                subject_id=request_id,
                target_id=project_row["id"],
                title="Project phase change executed",
                body=f"The project phase changed to {target_label}.",
                href=f"/projects/{project_row['slug']}",
            )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not persist phase vote activity") from exc

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_project["current_phase_id"],
    }


def create_project_update_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    body: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_governance_requests_allowed(project_row["project_mode"])
    _ensure_member(db, project_row["id"], current_user_id)
    member_count = _project_vote_population(db, project_row)

    try:
        created = db.execute(
            insert(project_update_requests)
            .values(
                project_id=project_row["id"],
                body=body.strip(),
                author_id=current_user_id,
                status="open",
            )
            .returning(
                project_update_requests.c.id,
                project_update_requests.c.project_id,
                project_update_requests.c.body,
                project_update_requests.c.author_id,
                project_update_requests.c.status,
                project_update_requests.c.created_at,
            )
        ).mappings().one()

        executed = False
        if member_count <= 1:
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == created["id"])
                .values(status="approved")
            )
            db.execute(
                insert(project_updates).values(
                    project_id=project_row["id"],
                    title="Approved update request",
                    body=created["body"],
                    author_id=created["author_id"],
                )
            )
            created = {**created, "status": "approved"}
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create update request") from exc

    summary = _compute_simple_vote_summary(db, project_update_request_votes, created["id"], member_count)
    return {"request": _serialize_update_request(created, summary), "executed": executed}


def vote_project_update_request(
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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"vote must be one of: {sorted(VALID_VOTES)}")

    request_row = db.execute(
        select(project_update_requests).where(
            project_update_requests.c.id == request_id,
            project_update_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project update request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project update request is already closed")

    existing_vote = db.execute(
        select(project_update_request_votes.c.vote).where(
            project_update_request_votes.c.request_id == request_id,
            project_update_request_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(project_update_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_update_request_votes)
                .where(
                    project_update_request_votes.c.request_id == request_id,
                    project_update_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_simple_vote_summary(db, project_update_request_votes, request_id, _project_vote_population(db, project_row))
        executed = False
        if summary["is_passing"]:
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                insert(project_updates).values(
                    project_id=project_row["id"],
                    title="Community update",
                    body=request_row["body"],
                    author_id=request_row["author_id"],
                )
            )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(project_update_requests)
                .where(project_update_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record update vote") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={"target_type": "project-update-request", "target_id": str(request_id), "vote": normalized_vote},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not persist update vote activity") from exc

    refreshed_request = db.execute(
        select(project_update_requests).where(project_update_requests.c.id == request_id)
    ).mappings().one()
    final_summary = _compute_simple_vote_summary(
        db,
        project_update_request_votes,
        request_id,
        _project_vote_population(db, project_row),
    )
    return {
        "request": _serialize_update_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }


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

    created = db.execute(
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
    ).mappings().one()
    db.commit()
    summary = _compute_simple_vote_summary(db, project_edit_request_votes, created["id"], _project_vote_population(db, project_row))
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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"vote must be one of: {sorted(VALID_VOTES)}")

    request_row = db.execute(
        select(project_edit_requests).where(
            project_edit_requests.c.id == request_id,
            project_edit_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project edit request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project edit request is already closed")

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

        summary = _compute_simple_vote_summary(db, project_edit_request_votes, request_id, _project_vote_population(db, project_row))
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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record edit vote") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={"target_type": "project-edit-request", "target_id": str(request_id), "vote": normalized_vote},
    )

    refreshed_request = db.execute(
        select(project_edit_requests).where(project_edit_requests.c.id == request_id)
    ).mappings().one()
    refreshed_project = db.execute(
        select(projects).where(projects.c.id == project_row["id"])
    ).mappings().one()
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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not persist edit vote activity") from exc
    return {
        "request": _serialize_edit_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }


def create_revert_phase_change_request(
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

    current_phase_id = str(project_row["current_phase_id"])
    if PHASE_ORDER[normalized_target] >= PHASE_ORDER[current_phase_id]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_phase_id must be earlier than current_phase_id for revert",
        )

    open_return = db.execute(
        select(project_phase_change_requests.c.id).where(
            project_phase_change_requests.c.project_id == project_row["id"],
            project_phase_change_requests.c.status == "open",
            project_phase_change_requests.c.change_kind == "return",
        )
    ).first()
    if open_return:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A vote is already open — approve or reject it first.",
        )

    created = db.execute(
        insert(project_phase_change_requests)
        .values(
            project_id=project_row["id"],
            from_phase_id=current_phase_id,
            target_phase_id=normalized_target,
            change_kind="return",
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

    summary = _compute_vote_summary(db, created["id"], _project_vote_population(db, project_row))
    return {"request": _serialize_phase_request(created, summary)}


def vote_revert_phase_change_request(
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
            project_phase_change_requests.c.change_kind == "return",
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revert phase request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revert phase request is already closed")

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

        summary = _compute_vote_summary(db, request_id, _project_vote_population(db, project_row))
        executed = False
        if summary["is_passing"]:
            target_phase_id = request_row["target_phase_id"]
            db.execute(
                update(project_phase_change_requests)
                .where(project_phase_change_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(
                    current_phase_id=target_phase_id,
                    stage_label=display_stage_label(
                        str(project_row["project_mode"]),
                        str(project_row["project_subtype"]) if project_row["project_subtype"] else None,
                        target_phase_id,
                    ),
                )
            )
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={"target_type": "project-phase-revert", "target_id": str(request_id), "vote": normalized_vote},
    )

    refreshed_request = db.execute(
        select(project_phase_change_requests).where(project_phase_change_requests.c.id == request_id)
    ).mappings().one()
    refreshed_project = db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    final_summary = _compute_vote_summary(db, request_id, _project_vote_population(db, refreshed_project))

    if executed:
        member_ids = db.execute(
            select(project_memberships.c.user_id).where(
                project_memberships.c.project_id == project_row["id"],
            )
        ).scalars().all()
        target_label = display_stage_label(
            str(refreshed_project["project_mode"]),
            str(refreshed_project["project_subtype"]) if refreshed_project.get("project_subtype") else None,
            str(refreshed_project["current_phase_id"]),
        )
        for member_id in member_ids:
            if member_id == current_user_id:
                continue
            create_notification(
                db=db,
                recipient_id=member_id,
                actor_id=current_user_id,
                kind="prj-phase-done",
                surface="project",
                subject_type="phase-change",
                subject_id=request_id,
                target_id=project_row["id"],
                title="Project phase change executed",
                body=f"The project phase changed to {target_label}.",
                href=f"/projects/{project_row['slug']}",
            )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not persist phase vote activity") from exc

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_project["current_phase_id"],
    }


def advance_project_phase(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    close_note: str | None = None,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    if project_row["project_mode"] == "personal-service":
        _ensure_manager(db, project_row["id"], current_user_id)
    else:
        _ensure_member(db, project_row["id"], current_user_id)

    current_phase_id = str(project_row["current_phase_id"])
    current_order = PHASE_ORDER.get(current_phase_id)
    if current_order is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project phase is invalid")

    next_phase_id = next_phase_id_for_project(
        str(project_row["project_mode"]),
        str(project_row["project_subtype"]) if project_row["project_subtype"] else None,
        current_phase_id,
    )
    if next_phase_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project is already in the final phase")

    _ensure_project_phase_plan_gate(db, project_row, next_phase_id)

    note = (close_note or "").strip()
    if next_phase_id == "phase-7" and not note:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="close_note is required when closing a project")

    try:
        resolved_subtype = str(project_row["project_subtype"]) if project_row["project_subtype"] else None
        update_values: dict[str, object] = {
            "current_phase_id": next_phase_id,
            "stage_label": display_stage_label(
                str(project_row["project_mode"]),
                resolved_subtype,
                next_phase_id,
            ),
        }

        # When advancing from proposal to production-plan, copy the subtype from
        # the winning plan into the project record.
        if current_phase_id == "phase-1" and next_phase_id == "phase-2":
            from app.services.projects_plans import _plan_subtype_from_payload

            winning_plan = db.execute(
                select(project_plans.c.project_subtype, project_plans.c.plan_payload)
                .where(
                    project_plans.c.project_id == project_row["id"],
                    project_plans.c.is_leading.is_(True),
                )
                .limit(1)
            ).mappings().first()
            if winning_plan:
                plan_subtype = winning_plan["project_subtype"] or _plan_subtype_from_payload(
                    dict(winning_plan["plan_payload"] or {})
                )
                if plan_subtype:
                    update_values["project_subtype"] = plan_subtype
                    resolved_subtype = plan_subtype

        db.execute(
            update(projects)
            .where(projects.c.id == project_row["id"])
            .values(**update_values)
        )

        if next_phase_id == "phase-7" and note:
            db.execute(
                insert(project_updates).values(
                    project_id=project_row["id"],
                    title="Closure note",
                    body=note,
                    author_id=current_user_id,
                )
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="advance-project-phase",
            metadata={"project_slug": project_slug, "from": current_phase_id, "to": next_phase_id},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not advance project phase") from exc

    return {
        "ok": True,
        "project_slug": project_row["slug"],
        "previous_phase_id": current_phase_id,
        "current_phase_id": next_phase_id,
    }
