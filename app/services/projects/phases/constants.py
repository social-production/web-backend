"""Shared constants for project phase governance."""

from __future__ import annotations

APPROVAL_THRESHOLD = 0.66
VALID_PHASE_IDS = frozenset(
    {"phase-1", "phase-2", "phase-3", "phase-4", "phase-5", "phase-6", "phase-7"}
)
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
