from __future__ import annotations

from app.services.projects.phases.constants import (
    PHASE_ORDER,
    STAGE_LABEL_BY_PHASE_ID,
)


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
        next_phase_id = next(
            (phase_id for phase_id, order in PHASE_ORDER.items() if order == next_order), None
        )
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
