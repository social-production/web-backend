from __future__ import annotations


def changed_text(proposed: str | None, current: object) -> str | None:
    """The trimmed proposal when it differs from the current value, else None (unchanged)."""
    cleaned = (proposed or "").strip()
    if not cleaned or cleaned == str(current or "").strip():
        return None
    return cleaned


def edit_changes(pairs: list[tuple[str, object, object]]) -> list[dict[str, object]]:
    """Before/after rows for an edit request, leaving out fields it doesn't change (None)."""
    return [
        {"label": label, "before": "" if before is None else str(before), "after": str(after)}
        for label, before, after in pairs
        if after is not None
    ]
