from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_memberships,
    event_plan_value_votes,
    event_plan_votes,
    event_plans,
    event_values,
    events,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.utils.votes import required_votes, resolve_event_vote_population

APPROVAL_THRESHOLD = 0.66
VALID_VOTES = {"yes", "no"}


def _serialize_plan(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "demand_consideration_note": row["demand_consideration_note"],
        "location_label": row["location_label"],
        "schedule_payload": row["schedule_payload"],
        "plan_payload": row["plan_payload"],
        "is_leading": row["is_leading"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _get_event_row_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(
        select(events).where(events.c.slug == slug.lower())
    ).mappings().first()
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only event members can submit or vote on plans")


def _compute_vote_summary(db: Session, plan_id: UUID, member_count: int) -> dict[str, object]:
    rows = db.execute(
        select(event_plan_votes.c.vote).where(event_plan_votes.c.plan_id == plan_id)
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
        "is_winning": meets_quorum and meets_approval,
    }


def submit_event_plan(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    title: str,
    description: str,
    demand_consideration_note: str,
    location_label: str,
    schedule_payload: dict[str, object],
    plan_payload: dict[str, object],
) -> dict[str, object]:
    event_row = _get_event_row_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    try:
        created = db.execute(
            insert(event_plans)
            .values(
                event_id=event_row["id"],
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                demand_consideration_note=demand_consideration_note.strip(),
                location_label=location_label.strip(),
                schedule_payload=schedule_payload,
                plan_payload=plan_payload,
                is_leading=False,
                status="open",
            )
            .returning(
                event_plans.c.id,
                event_plans.c.event_id,
                event_plans.c.title,
                event_plans.c.description,
                event_plans.c.author_id,
                event_plans.c.demand_consideration_note,
                event_plans.c.location_label,
                event_plans.c.schedule_payload,
                event_plans.c.plan_payload,
                event_plans.c.is_leading,
                event_plans.c.status,
                event_plans.c.created_at,
            )
        ).mappings().one()
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="submit-event-plan",
            metadata={"event_slug": event_slug, "plan_id": str(created["id"])},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not submit event plan") from exc

    vote_context_population = resolve_event_vote_population(db, event_row["id"])
    summary = _compute_vote_summary(db, created["id"], vote_context_population)
    return {"plan": _serialize_plan(created, summary)}


def list_event_plans(db: Session, event_slug: str) -> dict[str, object]:
    event_row = _get_event_row_by_slug(db, event_slug)
    member_count = resolve_event_vote_population(db, event_row["id"])

    rows = db.execute(
        select(event_plans)
        .where(event_plans.c.event_id == event_row["id"])
        .order_by(event_plans.c.created_at.desc())
    ).mappings().all()

    items: list[dict[str, object]] = []
    for row in rows:
        summary = _compute_vote_summary(db, row["id"], member_count)
        items.append(_serialize_plan(row, summary))

    return {
        "event_slug": event_row["slug"],
        "total": len(items),
        "items": items,
    }


def cast_event_plan_vote(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    plan_id: UUID,
    vote: str,
) -> dict[str, object]:
    event_row = _get_event_row_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    plan_row = db.execute(
        select(event_plans).where(
            event_plans.c.id == plan_id,
            event_plans.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    existing_vote = db.execute(
        select(event_plan_votes.c.vote).where(
            event_plan_votes.c.plan_id == plan_id,
            event_plan_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(event_plan_votes).values(
                    plan_id=plan_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(event_plan_votes)
                .where(
                    event_plan_votes.c.plan_id == plan_id,
                    event_plan_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        member_count = resolve_event_vote_population(db, event_row["id"])
        summary = _compute_vote_summary(db, plan_id, member_count)

        if summary["is_winning"]:
            db.execute(
                update(event_plans)
                .where(event_plans.c.event_id == event_row["id"])
                .values(is_leading=False)
            )
            db.execute(
                update(event_plans)
                .where(event_plans.c.id == plan_id)
                .values(is_leading=True, status="approved")
            )
            plan_is_leading = True
        else:
            db.execute(
                update(event_plans)
                .where(event_plans.c.id == plan_id)
                .values(is_leading=False)
            )
            plan_is_leading = False

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "event-plan", "target_id": str(plan_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast event plan vote") from exc

    refreshed_plan = db.execute(
        select(event_plans).where(event_plans.c.id == plan_id)
    ).mappings().one()
    final_summary = _compute_vote_summary(db, plan_id, resolve_event_vote_population(db, event_row["id"]))

    if plan_is_leading and plan_row["author_id"] is not None:
        create_notification(
            db=db,
            recipient_id=plan_row["author_id"],
            actor_id=current_user_id,
            kind="evt-plan-lead",
            surface="event",
            subject_type="event-plan",
            subject_id=plan_id,
            target_id=event_row["id"],
            title="Plan became leading",
            body="A vote passed and your event plan is now leading.",
            href=f"/events/{event_row['slug']}",
        )

    return {
        "plan": _serialize_plan(refreshed_plan, final_summary),
        "vote": normalized_vote,
        "is_leading": plan_is_leading,
    }


def cast_event_plan_value_vote(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    plan_id: UUID,
    value_id: UUID,
    vote: str,
) -> dict[str, object]:
    event_row = _get_event_row_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(VALID_VOTES)}",
        )

    plan_row = db.execute(
        select(event_plans.c.id).where(
            event_plans.c.id == plan_id,
            event_plans.c.event_id == event_row["id"],
        )
    ).first()
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    value_row = db.execute(
        select(event_values.c.id).where(
            event_values.c.id == value_id,
            event_values.c.event_id == event_row["id"],
        )
    ).first()
    if value_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event value not found")

    existing_vote = db.execute(
        select(event_plan_value_votes.c.vote).where(
            event_plan_value_votes.c.plan_id == plan_id,
            event_plan_value_votes.c.value_id == value_id,
            event_plan_value_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(event_plan_value_votes).values(
                    plan_id=plan_id,
                    value_id=value_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(event_plan_value_votes)
                .where(
                    event_plan_value_votes.c.plan_id == plan_id,
                    event_plan_value_votes.c.value_id == value_id,
                    event_plan_value_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "event-plan-value",
                "target_id": str(plan_id),
                "value_id": str(value_id),
                "vote": normalized_vote,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast event plan value vote") from exc

    return {
        "ok": True,
        "event_slug": event_row["slug"],
        "plan_id": plan_id,
        "value_id": value_id,
        "vote": normalized_vote,
    }
