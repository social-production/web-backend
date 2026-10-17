"""Opening a vote counts as voting yes on it."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException


def cast_proposer_yes(cast: Callable[[str], dict[str, object]]) -> dict[str, object] | None:
    """Cast the proposer's Yes; returns the vote result, or None when they can't vote."""
    try:
        return cast("yes")
    except HTTPException:
        return None
