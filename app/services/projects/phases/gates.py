from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_phase_change_votes,
    project_plans,
    projects,
)
from app.services.governance_votes import compute_vote_summary
from app.services.projects.phases.constants import (
    PHASE_ORDER,
)
from app.services.projects.phases.labels import _skips_distribution_phase
from app.utils.votes import resolve_project_vote_population


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


def _ensure_project_phase_plan_gate(
    db: Session, project_row: Mapping[str, object], target_phase_id: str
) -> None:
    current_phase_id = str(project_row["current_phase_id"])
    current_order = PHASE_ORDER.get(current_phase_id)
    target_order = PHASE_ORDER.get(target_phase_id)
    if current_order is None or target_order is None or target_order <= current_order:
        return

    project_subtype = (
        str(project_row["project_subtype"]) if project_row.get("project_subtype") else None
    )
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
    return compute_vote_summary(db, vote_table, request_id, member_count)


def _compute_vote_summary(db: Session, request_id: UUID, member_count: int) -> dict[str, object]:
    return compute_vote_summary(db, project_phase_change_votes, request_id, member_count)


def _ensure_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can request or vote"
        )


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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only project managers can perform this action",
        )


def _phase_change_kind_for_project(target_phase_id: str, current_phase_id: str) -> str:
    if target_phase_id == "phase-7":
        return "close"
    target_order = PHASE_ORDER.get(target_phase_id, 0)
    current_order = PHASE_ORDER.get(current_phase_id, 0)
    if target_order > 0 and current_order > 0 and target_order < current_order:
        return "return"
    return "advance"
