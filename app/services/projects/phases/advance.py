from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_plans,
    project_updates,
    projects,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.projects.phases.constants import PHASE_ORDER
from app.services.projects.phases.gates import (
    _ensure_manager,
    _ensure_member,
    _ensure_project_phase_plan_gate,
    _get_project_by_slug,
)
from app.services.projects.phases.labels import display_stage_label, next_phase_id_for_project


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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Project is already in the final phase"
        )

    _ensure_project_phase_plan_gate(db, project_row, next_phase_id)

    note = (close_note or "").strip()
    if next_phase_id == "phase-7" and not note:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="close_note is required when closing a project",
        )

    try:
        resolved_subtype = (
            str(project_row["project_subtype"]) if project_row["project_subtype"] else None
        )
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

            winning_plan = (
                db.execute(
                    select(project_plans.c.project_subtype, project_plans.c.plan_payload)
                    .where(
                        project_plans.c.project_id == project_row["id"],
                        project_plans.c.is_leading.is_(True),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if winning_plan:
                plan_subtype = winning_plan["project_subtype"] or _plan_subtype_from_payload(
                    dict(winning_plan["plan_payload"] or {})
                )
                if plan_subtype:
                    update_values["project_subtype"] = plan_subtype
                    resolved_subtype = plan_subtype

        db.execute(
            update(projects).where(projects.c.id == project_row["id"]).values(**update_values)
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not advance project phase",
        ) from exc

    return {
        "ok": True,
        "project_slug": project_row["slug"],
        "previous_phase_id": current_phase_id,
        "current_phase_id": next_phase_id,
    }
