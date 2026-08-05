from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    event_editors,
    event_memberships,
    events,
)
from app.services.events.helpers import _get_signal_counts_db
from app.services.events.phases.constants import EVENT_PHASE_ORDER
from app.services.governance_votes import compute_vote_summary
from app.services.signal_gates import ensure_proposal_advancement_allowed
from app.utils.votes import (
    can_cast_event_governance_vote,
    is_platform_event,
    required_votes,
    resolve_event_vote_population,
)


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


def _ensure_can_cast_governance_vote(
    db: Session,
    event_row: Mapping[str, object],
    user_id: UUID,
    *,
    detail: str = "Only event members can request or vote",
) -> None:
    """Platform-tagged events open governance votes to any signed-in user."""
    if can_cast_event_governance_vote(db, event_id=event_row["id"], user_id=user_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _is_event_organizer(db: Session, event_row: Mapping[str, object], user_id: UUID) -> bool:
    if event_row.get("created_by") == user_id:
        return True
    row = db.execute(
        select(event_editors.c.user_id).where(
            event_editors.c.event_id == event_row["id"],
            event_editors.c.user_id == user_id,
        )
    ).first()
    return row is not None


def _is_organizer_controlled(event_row: Mapping[str, object]) -> bool:
    return event_row.get("governance") == "organizer_controlled"


def _ensure_can_drive_lifecycle(
    db: Session, event_row: Mapping[str, object], user_id: UUID
) -> None:
    """Only members can act, but organizer-controlled events restrict this to organizers."""
    if _is_organizer_controlled(event_row):
        if not _is_event_organizer(db, event_row, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the creator or co-organizers can manage this organizer-controlled event",
            )
        return
    _ensure_member(db, event_row["id"], user_id)


def _compute_votes(
    db: Session,
    table,
    request_id: UUID,
    member_count: int,
) -> dict[str, object]:
    return compute_vote_summary(db, table, request_id, member_count)


def _ensure_event_proposal_signal_gate(
    db: Session,
    event_row: Mapping[str, object],
    *,
    current_phase_id: str,
    change_kind: str,
) -> None:
    if current_phase_id != "proposal" or change_kind != "advance":
        return

    event_id = event_row["id"]
    uses_platform = is_platform_event(db, event_id)
    population = resolve_event_vote_population(db, event_id)
    signal_counts = _get_signal_counts_db(db, event_id)
    ensure_proposal_advancement_allowed(
        signal_counts,
        required_demand=required_votes(population),
        uses_platform_vote_context=uses_platform,
    )


def _phase_change_kind_for_event(target_phase_id: str, current_phase_id: str) -> str:
    if target_phase_id == "closed":
        return "close"
    target_order = EVENT_PHASE_ORDER.get(target_phase_id, 0)
    current_order = EVENT_PHASE_ORDER.get(current_phase_id, 0)
    if target_order > 0 and current_order > 0 and target_order < current_order:
        return "return"
    return "advance"
