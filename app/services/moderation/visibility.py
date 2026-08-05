from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from app.models import comments, events, help_requests, messages, posts, projects, threads


def _table_for_entity(entity_type: str):
    return {
        "post": posts,
        "thread": threads,
        "project": projects,
        "event": events,
        "help_request": help_requests,
        "comment": comments,
        "message": messages,
    }.get(entity_type)


def not_removed_clause(entity_type: str) -> ColumnElement[bool] | None:
    table = _table_for_entity(entity_type)
    if table is None or not hasattr(table.c, "moderation_state"):
        return None
    return table.c.moderation_state != "removed"


def assert_not_removed(db: Session, entity_type: str, entity_id: UUID) -> None:
    table = _table_for_entity(entity_type)
    if table is None or not hasattr(table.c, "moderation_state"):
        return
    row = (
        db.execute(select(table.c.moderation_state).where(table.c.id == entity_id))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity_type.capitalize()} not found"
        )
    if row["moderation_state"] == "removed":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity_type.capitalize()} not found"
        )


def moderation_fields(row: dict[str, object] | object) -> dict[str, object]:
    if hasattr(row, "get"):
        state = row.get("moderation_state") or "visible"  # type: ignore[index]
        reason = row.get("moderation_reason")  # type: ignore[index]
    else:
        state = getattr(row, "moderation_state", "visible")
        reason = getattr(row, "moderation_reason", None)
    return {
        "moderationState": state,
        "moderationReason": reason,
        "isRemovedByReport": state == "removed",
        "isUnderReview": state == "under_review",
    }
