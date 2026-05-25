from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_edit_request_votes,
    event_edit_requests,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_update_request_votes,
    event_update_requests,
    event_updates,
    events,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document
from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66
VALID_PHASE_IDS = frozenset({"phase-1", "phase-2", "phase-3", "phase-4", "phase-5", "phase-6", "phase-7"})
VALID_VOTES = frozenset({"yes", "no"})


def _get_event_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return row


def _ensure_member(db: Session, event_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == event_id,
            event_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only event members can request or vote")


def _compute_votes(
    db: Session,
    table,
    request_id: UUID,
    member_count: int,
) -> dict[str, object]:
    rows = db.execute(
        select(table.c.vote).where(table.c.request_id == request_id)
    ).all()

    yes_count = 0
    no_count = 0
    for (vote,) in rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1

    total_votes = yes_count + no_count
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    votes_required = required_votes(member_count)
    meets_quorum = total_votes >= votes_required
    meets_approval = approval_ratio >= APPROVAL_THRESHOLD

    return {
        "yes_count": yes_count,
        "no_count": no_count,
        "total_votes": total_votes,
        "approval_ratio": approval_ratio,
        "approval_threshold": APPROVAL_THRESHOLD,
        "votes_required": votes_required,
        "member_count": member_count,
        "meets_quorum": meets_quorum,
        "meets_approval": meets_approval,
        "is_passing": meets_quorum and meets_approval,
    }


def _serialize_phase_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "from_phase_id": row["from_phase_id"],
        "target_phase_id": row["target_phase_id"],
        "change_kind": row["change_kind"],
        "reason": row["reason"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _serialize_update_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "body": row["body"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _serialize_edit_request(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


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

    try:
        created = db.execute(
            insert(event_phase_change_requests)
            .values(
                event_id=event_row["id"],
                from_phase_id=event_row["current_phase_id"],
                target_phase_id=normalized_target,
                change_kind="advance",
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
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create phase request") from exc

    summary = _compute_votes(db, event_phase_change_votes, created["id"], int(event_row["member_count"] or 0))
    return {"request": _serialize_phase_request(created, summary)}


def list_phase_change_requests(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    member_count = int(event_row["member_count"] or 0)

    rows = db.execute(
        select(event_phase_change_requests)
        .where(event_phase_change_requests.c.event_id == event_row["id"])
        .order_by(event_phase_change_requests.c.created_at.desc())
    ).mappings().all()

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

    request_row = db.execute(
        select(event_phase_change_requests).where(
            event_phase_change_requests.c.id == request_id,
            event_phase_change_requests.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phase change request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phase change request is already closed")

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

        summary = _compute_votes(db, event_phase_change_votes, request_id, int(event_row["member_count"] or 0))

        executed = False
        if summary["is_passing"]:
            target_phase_id = request_row["target_phase_id"]
            db.execute(
                update(event_phase_change_requests)
                .where(event_phase_change_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(current_phase_id=target_phase_id)
            )
            executed = True

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "event-phase-change", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record phase vote") from exc

    refreshed_request = db.execute(
        select(event_phase_change_requests).where(event_phase_change_requests.c.id == request_id)
    ).mappings().one()
    refreshed_event = db.execute(select(events).where(events.c.id == event_row["id"])).mappings().one()
    final_summary = _compute_votes(db, event_phase_change_votes, request_id, int(refreshed_event["member_count"] or 0))

    if executed and request_row["author_id"] is not None:
        create_notification(
            db=db,
            recipient_id=request_row["author_id"],
            actor_id=current_user_id,
            kind="evt-phase-done",
            surface="event",
            subject_type="phase-change",
            subject_id=request_id,
            target_id=event_row["id"],
            title="Event phase change executed",
            body=f"The event phase changed to {refreshed_event['current_phase_id']}.",
            href=f"/events/{event_row['slug']}",
        )

    return {
        "request": _serialize_phase_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
        "current_phase_id": refreshed_event["current_phase_id"],
    }


def create_update_request(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    body: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    try:
        created = db.execute(
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
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create update request") from exc

    summary = _compute_votes(db, event_update_request_votes, created["id"], int(event_row["member_count"] or 0))
    return {"request": _serialize_update_request(created, summary)}


def list_update_requests(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    member_count = int(event_row["member_count"] or 0)

    rows = db.execute(
        select(event_update_requests)
        .where(event_update_requests.c.event_id == event_row["id"])
        .order_by(event_update_requests.c.created_at.desc())
    ).mappings().all()

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

    request_row = db.execute(
        select(event_update_requests).where(
            event_update_requests.c.id == request_id,
            event_update_requests.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Update request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Update request is already closed")

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

        summary = _compute_votes(db, event_update_request_votes, request_id, int(event_row["member_count"] or 0))

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

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "event-update-request", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record update vote") from exc

    refreshed_request = db.execute(
        select(event_update_requests).where(event_update_requests.c.id == request_id)
    ).mappings().one()
    final_summary = _compute_votes(db, event_update_request_votes, request_id, int(event_row["member_count"] or 0))

    return {
        "request": _serialize_update_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }


def create_edit_request(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    title: str,
    description: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    try:
        created = db.execute(
            insert(event_edit_requests)
            .values(
                event_id=event_row["id"],
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                status="open",
            )
            .returning(
                event_edit_requests.c.id,
                event_edit_requests.c.event_id,
                event_edit_requests.c.title,
                event_edit_requests.c.description,
                event_edit_requests.c.author_id,
                event_edit_requests.c.status,
                event_edit_requests.c.created_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create edit request") from exc

    summary = _compute_votes(db, event_edit_request_votes, created["id"], int(event_row["member_count"] or 0))
    return {"request": _serialize_edit_request(created, summary)}


def list_edit_requests(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug(db, event_slug)
    member_count = int(event_row["member_count"] or 0)

    rows = db.execute(
        select(event_edit_requests)
        .where(event_edit_requests.c.event_id == event_row["id"])
        .order_by(event_edit_requests.c.created_at.desc())
    ).mappings().all()

    items = []
    for row in rows:
        summary = _compute_votes(db, event_edit_request_votes, row["id"], member_count)
        items.append(_serialize_edit_request(row, summary))

    return {
        "event_slug": event_row["slug"],
        "total": len(items),
        "items": items,
    }


def vote_edit_request(
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

    request_row = db.execute(
        select(event_edit_requests).where(
            event_edit_requests.c.id == request_id,
            event_edit_requests.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Edit request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Edit request is already closed")

    existing_vote = db.execute(
        select(event_edit_request_votes.c.vote).where(
            event_edit_request_votes.c.request_id == request_id,
            event_edit_request_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(event_edit_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(event_edit_request_votes)
                .where(
                    event_edit_request_votes.c.request_id == request_id,
                    event_edit_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = _compute_votes(db, event_edit_request_votes, request_id, int(event_row["member_count"] or 0))

        executed = False
        if summary["is_passing"]:
            db.execute(
                update(event_edit_requests)
                .where(event_edit_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(
                    title=request_row["title"],
                    description=request_row["description"],
                )
            )
            executed = True

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "event-edit-request", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record edit vote") from exc

    refreshed_request = db.execute(
        select(event_edit_requests).where(event_edit_requests.c.id == request_id)
    ).mappings().one()
    refreshed_event = db.execute(select(events).where(events.c.id == event_row["id"])).mappings().one()
    final_summary = _compute_votes(db, event_edit_request_votes, request_id, int(event_row["member_count"] or 0))

    if executed:
        index_document(
            db=db,
            entity_type="event",
            entity_id=event_row["id"],
            title=str(refreshed_event["title"]),
            summary=str(refreshed_event["description"]),
            meta="event",
            href=f"/events/{event_row['slug']}",
        )

    return {
        "request": _serialize_edit_request(refreshed_request, final_summary),
        "vote": normalized_vote,
        "executed": executed,
    }
