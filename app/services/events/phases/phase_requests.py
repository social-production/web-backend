from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    events,
)
from app.services.events.phases.constants import VALID_PHASE_IDS, VALID_VOTES
from app.services.events.phases.gates import (
    _compute_votes,
    _ensure_member,
    _event_vote_population,
    _get_event_by_slug,
    _phase_change_kind_for_event,
)
from app.services.events.phases.serializers import _serialize_phase_request
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification


def create_phase_change_request(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    target_phase_id: str,
    reason: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    normalized_target = target_phase_id.strip().lower()
    if normalized_target not in VALID_PHASE_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_phase_id must be one of: {sorted(VALID_PHASE_IDS)}",
        )

    if normalized_target == event_row["current_phase_id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_phase_id must differ from current_phase_id",
        )

    change_kind = _phase_change_kind_for_event(normalized_target, event_row["current_phase_id"])

    open_request = db.execute(
        select(event_phase_change_requests.c.id).where(
            event_phase_change_requests.c.event_id == event_row["id"],
            event_phase_change_requests.c.status == "open",
            event_phase_change_requests.c.target_phase_id == normalized_target,
        )
    ).first()
    if open_request:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A vote is already open — approve or reject it first.",
        )

    try:
        created = (
            db.execute(
                insert(event_phase_change_requests)
                .values(
                    event_id=event_row["id"],
                    from_phase_id=event_row["current_phase_id"],
                    target_phase_id=normalized_target,
                    change_kind=change_kind,
                    reason=reason.strip(),
                    author_id=current_user_id,
                    status="open",
                )
                .returning(
                    event_phase_change_requests.c.id,
                    event_phase_change_requests.c.event_id,
                    event_phase_change_requests.c.from_phase_id,
                    event_phase_change_requests.c.target_phase_id,
                    event_phase_change_requests.c.change_kind,
                    event_phase_change_requests.c.reason,
                    event_phase_change_requests.c.author_id,
                    event_phase_change_requests.c.status,
                    event_phase_change_requests.c.created_at,
                )
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create phase request",
        ) from exc

    summary = _compute_votes(
        db, event_phase_change_votes, created["id"], _event_vote_population(db, event_row)
    )
    member_ids = (
        db.execute(
            select(event_memberships.c.user_id).where(
                event_memberships.c.event_id == event_row["id"]
            )
        )
        .scalars()
        .all()
    )
    target_label = normalized_target.replace("-", " ").title()
    for member_id in member_ids:
        if member_id == current_user_id:
            continue
        create_notification(
            db=db,
            recipient_id=member_id,
            actor_id=current_user_id,
            kind="evt-phase-vote",
            surface="event",
            subject_type="phase-change",
            subject_id=created["id"],
            target_id=event_row["id"],
            title="Event phase vote open",
            body=f"Vote on advancing to {target_label}.",
            href=f"/events/{event_row['slug']}?open=vote&voteKind=phase_change&voteTarget={created['id']}",
        )
    db.commit()
    return {"request": _serialize_phase_request(created, summary)}


def list_phase_change_requests(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    member_count = _event_vote_population(db, event_row)

    rows = (
        db.execute(
            select(event_phase_change_requests)
            .where(event_phase_change_requests.c.event_id == event_row["id"])
            .order_by(event_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    items = []
    for row in rows:
        summary = _compute_votes(db, event_phase_change_votes, row["id"], member_count)
        items.append(_serialize_phase_request(row, summary))

    return {
        "event_slug": event_row["slug"],
        "current_phase_id": event_row["current_phase_id"],
        "total": len(items),
        "items": items,
    }


def vote_phase_change_request(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    request_row = (
        db.execute(
            select(event_phase_change_requests).where(
                event_phase_change_requests.c.id == request_id,
                event_phase_change_requests.c.event_id == event_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Phase change request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Phase change request is already closed"
        )

    existing_vote = db.execute(
        select(event_phase_change_votes.c.vote).where(
            event_phase_change_votes.c.request_id == request_id,
            event_phase_change_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(event_phase_change_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(event_phase_change_votes)
                .where(
                    event_phase_change_votes.c.request_id == request_id,
                    event_phase_change_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_votes(
            db, event_phase_change_votes, request_id, _event_vote_population(db, event_row)
        )

        executed = False
        if summary["is_passing"]:
            target_phase_id = request_row["target_phase_id"]
            db.execute(
                update(event_phase_change_requests)
                .where(event_phase_change_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(event_phase_change_requests)
                .where(
                    event_phase_change_requests.c.event_id == event_row["id"],
                    event_phase_change_requests.c.id != request_id,
                    event_phase_change_requests.c.status == "open",
                )
                .values(status="closed")
            )
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(current_phase_id=target_phase_id)
            )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(event_phase_change_requests)
                .where(event_phase_change_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote"
        ) from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "event-phase-change",
            "target_id": str(request_id),
            "vote": normalized_vote,
        },
    )

    refreshed_request = (
        db.execute(
            select(event_phase_change_requests).where(
                event_phase_change_requests.c.id == request_id
            )
        )
        .mappings()
        .one()
    )
    refreshed_event = (
        db.execute(select(events).where(events.c.id == event_row["id"])).mappings().one()
    )
    final_summary = _compute_votes(
        db, event_phase_change_votes, request_id, _event_vote_population(db, refreshed_event)
    )

    if executed:
        member_ids = (
            db.execute(
                select(event_memberships.c.user_id).where(
                    event_memberships.c.event_id == event_row["id"]
                )
            )
            .scalars()
            .all()
        )
        target_label = str(refreshed_event["current_phase_id"]).replace("-", " ").title()
        for member_id in member_ids:
            if member_id == current_user_id:
                continue
            create_notification(
                db=db,
                recipient_id=member_id,
                actor_id=current_user_id,
                kind="evt-phase-done",
                surface="event",
                subject_type="phase-change",
                subject_id=request_id,
                target_id=event_row["id"],
                title="Event phase change executed",
                body=f"The event phase changed to {target_label}.",
                href=f"/events/{event_row['slug']}",
            )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not persist phase vote activity",
        ) from exc

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_event["current_phase_id"],
    }
