"""Canonical handle policy for usernames, channels, and communities.

Rules (enforced on create/register only — existing invalid rows are grandfathered):
- length 3–32
- letters (preserve entered case), digits, hyphen, underscore
- no spaces
- no leading, trailing, or repeated separators (`-` / `_`)
"""

from __future__ import annotations

import re

from fastapi import HTTPException, status

HANDLE_MIN_LENGTH = 3
HANDLE_MAX_LENGTH = 32
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")

HANDLE_ERROR_GENERIC = (
    "Use 3–32 characters: letters, numbers, hyphens, or underscores. "
    "No spaces, and no leading, trailing, or repeated separators."
)


def canonicalize_handle(value: str) -> str:
    return value.strip().lower()


def validate_handle(value: str, *, field_label: str = "Handle") -> str:
    """Validate and return the display handle (case preserved, edges stripped)."""
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_label} is required",
        )

    display = value.strip()
    if not display:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_label} is required",
        )

    if any(ch.isspace() for ch in display):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_label} cannot contain spaces",
        )

    if len(display) < HANDLE_MIN_LENGTH or len(display) > HANDLE_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_label} must be {HANDLE_MIN_LENGTH}–{HANDLE_MAX_LENGTH} characters",
        )

    if (
        display.startswith(("-", "_"))
        or display.endswith(("-", "_"))
        or "--" in display
        or "__" in display
        or "-_" in display
        or "_-" in display
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=HANDLE_ERROR_GENERIC,
        )

    if not HANDLE_PATTERN.fullmatch(display):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=HANDLE_ERROR_GENERIC,
        )

    return display


def handles_conflict(left: str, right: str) -> bool:
    return canonicalize_handle(left) == canonicalize_handle(right)
