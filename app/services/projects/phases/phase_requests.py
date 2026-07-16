from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_updates,
    projects,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.projects.phases.constants import VALID_PHASE_IDS, VALID_VOTES
from app.services.projects.phases.gates import (
    _compute_vote_summary,
    _ensure_member,
    _ensure_phase_requests_allowed,
    _ensure_project_phase_plan_gate,
    _get_project_by_slug,
    _phase_change_kind_for_project,
    _project_vote_population,
)
from app.services.projects.phases.labels import display_stage_label
from app.services.projects.phases.serializers import _serialize_phase_request


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
        created = (
            db.execute(
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
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create phase request",
        ) from exc

    summary = _compute_vote_summary(db, created["id"], _project_vote_population(db, project_row))
    member_ids = (
        db.execute(
            select(project_memberships.c.user_id).where(
                project_memberships.c.project_id == project_row["id"],
            )
        )
        .scalars()
        .all()
    )
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
    rows = (
        db.execute(
            select(project_phase_change_requests)
            .where(project_phase_change_requests.c.project_id == project_row["id"])
            .order_by(project_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

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

    request_row = (
        db.execute(
            select(project_phase_change_requests).where(
                project_phase_change_requests.c.id == request_id,
                project_phase_change_requests.c.project_id == project_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Phase change request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Phase change request is already closed"
        )

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
                        str(project_row["project_subtype"])
                        if project_row["project_subtype"]
                        else None,
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote"
        ) from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "project-phase-change",
            "target_id": str(request_id),
            "vote": normalized_vote,
        },
    )

    refreshed_request = (
        db.execute(
            select(project_phase_change_requests).where(
                project_phase_change_requests.c.id == request_id
            )
        )
        .mappings()
        .one()
    )
    refreshed_project = (
        db.execute(select(projects).where(projects.c.id == project_row["id"])).mappings().one()
    )
    final_summary = _compute_vote_summary(
        db, request_id, _project_vote_population(db, refreshed_project)
    )

    if executed:
        member_ids = (
            db.execute(
                select(project_memberships.c.user_id).where(
                    project_memberships.c.project_id == project_row["id"],
                )
            )
            .scalars()
            .all()
        )
        target_label = display_stage_label(
            str(refreshed_project["project_mode"]),
            str(refreshed_project["project_subtype"])
            if refreshed_project.get("project_subtype")
            else None,
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not persist phase vote activity",
        ) from exc

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_project["current_phase_id"],
    }
