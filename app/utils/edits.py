from __future__ import annotations


def changed_text(proposed: str | None, current: object) -> str | None:
    """The trimmed proposal when it differs from the current value, else None (unchanged)."""
    cleaned = (proposed or "").strip()
    if not cleaned or cleaned == str(current or "").strip():
        return None
    return cleaned
