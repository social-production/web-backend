from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.models import (
    comments,
    events,
    help_requests,
    messages,
    posts,
    projects,
    searchable_documents,
    threads,
)
from app.services.moderation.thresholds import TOP_LEVEL_TARGET_TYPES


def _table_for_target(target_type: str):
    return {
        "post": posts,
        "thread": threads,
        "project": projects,
        "event": events,
        "help_request": help_requests,
        "comment": comments,
        "message": messages,
    }.get(target_type)


def apply_moderation_state(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    moderation_state: str,
    moderation_reason: str | None,
) -> None:
    table = _table_for_target(target_type)
    if table is None:
        return
    db.execute(
        update(table)
        .where(table.c.id == target_id)
        .values(
            moderation_state=moderation_state,
            moderation_reason=moderation_reason,
        )
    )


def _unindex_search_document(db: Session, *, target_type: str, target_id: UUID) -> None:
    db.execute(
        delete(searchable_documents).where(
            searchable_documents.c.entity_type == target_type,
            searchable_documents.c.entity_id == target_id,
        )
    )


def apply_resolution_effect(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    reason: str,
    resolution: str,
) -> None:
    normalized = target_type.strip().lower()

    if resolution == "dismissed":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="visible",
            moderation_reason=None,
        )
        return

    if resolution == "under_review":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="under_review",
            moderation_reason=reason,
        )
        return

    if resolution == "hidden":
        # Serious-harm restriction applies on all surfaces: message-like bodies
        # hide/reveal, top-level content blurs with a warning until deletion.
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="hidden",
            moderation_reason=reason,
        )
        return

    if resolution == "removed":
        apply_moderation_state(
            db,
            target_type=normalized,
            target_id=target_id,
            moderation_state="removed",
            moderation_reason=reason,
        )
        if normalized in TOP_LEVEL_TARGET_TYPES:
            _unindex_search_document(db, target_type=normalized, target_id=target_id)
