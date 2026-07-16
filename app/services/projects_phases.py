"""Backward-compatible re-exports for project phase governance."""

from __future__ import annotations

from app.services.projects.phases import (
    advance_project_phase,
    create_phase_change_request,
    create_project_edit_request,
    create_project_update_request,
    create_revert_phase_change_request,
    display_stage_label,
    effective_phase_id_for_progress,
    lifecycle_phase_title,
    list_phase_change_requests,
    next_phase_id_for_project,
    visible_phase_ids_for_project,
    vote_phase_change_request,
    vote_project_edit_request,
    vote_project_update_request,
    vote_revert_phase_change_request,
)
from app.services.projects.phases.gates import (
    _compute_simple_vote_summary,
    _compute_vote_summary,
    _ensure_governance_requests_allowed,
    _ensure_manager,
    _ensure_member,
    _ensure_phase_requests_allowed,
    _ensure_project_phase_plan_gate,
    _get_project_by_slug,
    _phase_change_kind_for_project,
    _project_vote_population,
    _required_leading_plan_kind,
)
from app.services.projects.phases.labels import _skips_distribution_phase
from app.services.projects.phases.serializers import (
    _serialize_edit_request,
    _serialize_phase_request,
    _serialize_update_request,
)

__all__ = [
    "_compute_simple_vote_summary",
    "_compute_vote_summary",
    "_ensure_governance_requests_allowed",
    "_ensure_manager",
    "_ensure_member",
    "_ensure_phase_requests_allowed",
    "_ensure_project_phase_plan_gate",
    "_get_project_by_slug",
    "_phase_change_kind_for_project",
    "_project_vote_population",
    "_required_leading_plan_kind",
    "_serialize_edit_request",
    "_serialize_phase_request",
    "_serialize_update_request",
    "_skips_distribution_phase",
    "advance_project_phase",
    "create_phase_change_request",
    "create_project_edit_request",
    "create_project_update_request",
    "create_revert_phase_change_request",
    "display_stage_label",
    "effective_phase_id_for_progress",
    "lifecycle_phase_title",
    "list_phase_change_requests",
    "next_phase_id_for_project",
    "visible_phase_ids_for_project",
    "vote_phase_change_request",
    "vote_project_edit_request",
    "vote_project_update_request",
    "vote_revert_phase_change_request",
]
