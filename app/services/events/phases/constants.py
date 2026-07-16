"""Shared constants for event phase governance."""

from __future__ import annotations

VALID_PHASE_IDS = frozenset({"proposal", "event-plan", "activity", "closed"})
EVENT_PHASE_ORDER = {
    "proposal": 1,
    "event-plan": 2,
    "activity": 3,
    "closed": 4,
}
VALID_VOTES = frozenset({"yes", "no"})
