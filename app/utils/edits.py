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


def ensure_no_overlapping_edit(db, table, owner_column, owner_id, proposed: dict) -> None:
    """Refuse an edit that changes a field another open edit is already changing.

    ``proposed`` maps column names to the new value, or None when the edit leaves it alone.
    """
    from fastapi import HTTPException, status
    from sqlalchemy import select

    changing = [column for column, value in proposed.items() if value is not None]
    if not changing:
        return
    open_rows = db.execute(
        select(*(table.c[column] for column in changing)).where(
            owner_column == owner_id, table.c.status == "open"
        )
    ).all()
    for open_row in open_rows:
        overlap = [column for column, value in zip(changing, open_row) if value is not None]
        if overlap:
            names = " and ".join(
                column.replace("_label", "").replace("_", " ") for column in overlap
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An edit to the {names} is already being voted on. Vote on it first.",
            )
