"""Shared constants for software project governance."""

from __future__ import annotations

APPROVAL_THRESHOLD_PERCENT = 66.0
APPROVAL_THRESHOLD_RATIO = 0.66
VALID_VOTES = frozenset({"yes", "no", "neutral"})
VALID_ACTIONS = frozenset({"grant", "revoke"})

PR_STAGE_LABELS: dict[str, str] = {
    "approval": "Vote needed",
    "awaiting-merge": "Merge needed",
    "confirmation": "Merge confirmation needed",
    "confirmed": "Merged",
    "rejected": "Rejected",
    "replaced": "Replaced",
}
