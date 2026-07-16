from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_update_request_votes,
    event_update_requests,
    event_updates,
)
from app.services.events.phases.constants import VALID_VOTES
from app.services.events.phases.gates import (
    _compute_votes,
    _ensure_member,
    _event_vote_population,
    _get_event_by_slug,
)
from app.services.events.phases.serializers import _serialize_update_request
from app.services.meaningful_actions import record_meaningful_action


def create_update_request(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    body: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)
    member_count = _event_vote_population(db, event_row)

    try:
        created = (
            db.execute(
                insert(event_update_requests)
                .values(
                    event_id=event_row["id"],
                    body=body.strip(),
                    author_id=current_user_id,
                    status="open",
                )
                .returning(
                    event_update_requests.c.id,
                    event_update_requests.c.event_id,
                    event_update_requests.c.body,
                    event_update_requests.c.author_id,
                    event_update_requests.c.status,
                    event_update_requests.c.created_at,
                )
            )
            .mappings()
            .one()
        )

        executed = False
        if member_count <= 1:
            db.execute(
                update(event_update_requests)
                .where(event_update_requests.c.id == created["id"])
                .values(status="approved")
            )
            db.execute(
                insert(event_updates).values(
                    event_id=event_row["id"],
                    title="Approved update request",
                    body=created["body"],
                    author_id=created["author_id"],
                )
            )
            created = {**created, "status": "approved"}
            executed = True

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create update request",
        ) from exc

    summary = _compute_votes(db, event_update_request_votes, created["id"], member_count)
    return {"request": _serialize_update_request(created, summary), "executed": executed}


def list_update_requests(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    member_count = _event_vote_population(db, event_row)

    rows = (
        db.execute(
            select(event_update_requests)
            .where(event_update_requests.c.event_id == event_row["id"])
            .order_by(event_update_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    items = []
    for row in rows:
        summary = _compute_votes(db, event_update_request_votes, row["id"], member_count)
        items.append(_serialize_update_request(row, summary))

    return {
        "event_slug": event_row["slug"],
        "total": len(items),
        "items": items,
    }


def vote_update_request(
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
            select(event_update_requests).where(
                event_update_requests.c.id == request_id,
                event_update_requests.c.event_id == event_row["id"],
            )
        )
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Update request not found"
        )
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Update request is already closed"
        )

    existing_vote = db.execute(
        select(event_update_request_votes.c.vote).where(
            event_update_request_votes.c.request_id == request_id,
            event_update_request_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(event_update_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(event_update_request_votes)
                .where(
                    event_update_request_votes.c.request_id == request_id,
                    event_update_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_votes(
            db, event_update_request_votes, request_id, _event_vote_population(db, event_row)
        )

        executed = False
        if summary["is_passing"]:
            db.execute(
                update(event_update_requests)
                .where(event_update_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                insert(event_updates).values(
                    event_id=event_row["id"],
                    title="Approved update request",
                    body=request_row["body"],
                    author_id=request_row["author_id"],
                )
            )
            executed = True
        elif not summary.get("can_still_pass", True):
            db.execute(
                update(event_update_requests)
                .where(event_update_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record update vote"
        ) from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "event-update-request",
            "target_id": str(request_id),
            "vote": normalized_vote,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not persist update vote activity",
        ) from exc

    refreshed_request = (
        db.execute(select(event_update_requests).where(event_update_requests.c.id == request_id))
        .mappings()
        .one()
    )
    final_summary = _compute_votes(
        db, event_update_request_votes, request_id, _event_vote_population(db, event_row)
    )

    return {
        "request": _serialize_update_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }
