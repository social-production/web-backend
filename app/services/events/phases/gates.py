from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    event_memberships,
    events,
)
from app.services.events.phases.constants import EVENT_PHASE_ORDER
from app.services.governance_votes import compute_vote_summary
from app.utils.votes import resolve_event_vote_population


def _get_event_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return row


def _event_vote_population(db: Session, event_row: Mapping[str, object]) -> int:
    return resolve_event_vote_population(db, event_row["id"])


def _ensure_member(db: Session, event_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == event_id,
            event_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only event members can request or vote"
        )


def _compute_votes(
    db: Session,
    table,
    request_id: UUID,
    member_count: int,
) -> dict[str, object]:
    return compute_vote_summary(db, table, request_id, member_count)


def _phase_change_kind_for_event(target_phase_id: str, current_phase_id: str) -> str:
    if target_phase_id == "closed":
        return "close"
    target_order = EVENT_PHASE_ORDER.get(target_phase_id, 0)
    current_order = EVENT_PHASE_ORDER.get(current_phase_id, 0)
    if target_order > 0 and current_order > 0 and target_order < current_order:
        return "return"
    return "advance"
