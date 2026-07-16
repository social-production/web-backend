from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_memberships,
    event_plan_criterion_ratings,
    event_plan_value_votes,
    event_plan_votes,
    event_plans,
    event_values,
    events,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.plan_criteria import (
    VALID_PLAN_RATINGS,
    assessment_criteria_for_plan,
    parse_value_criterion_id,
)
from app.utils.votes import required_votes, resolve_event_vote_population

APPROVAL_THRESHOLD = 0.66
VALID_VOTES = {"yes", "no", "neutral"}


def _schedule_start_utc_from_payload(schedule_payload: dict[str, object]) -> datetime | None:
    start_at_utc = schedule_payload.get("startAtUtc") or schedule_payload.get("start_at_utc")

    if not start_at_utc:
        return None

    try:
        parsed = datetime.fromisoformat(str(start_at_utc).replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _sync_event_schedule_from_leading_plan(
    db: Session,
    event_id: UUID,
    plan_id: UUID,
) -> None:
    plan_row = db.execute(
        select(
            event_plans.c.schedule_payload,
            event_plans.c.location_label,
        ).where(event_plans.c.id == plan_id)
    ).mappings().one()

    schedule_payload = dict(plan_row["schedule_payload"] or {})
    scheduled_at = _schedule_start_utc_from_payload(schedule_payload)
    update_values: dict[str, object] = {}

    if scheduled_at is not None:
        update_values["scheduled_at"] = scheduled_at

    location_label = str(plan_row["location_label"] or "").strip()
    if location_label:
        update_values["location_label"] = location_label

    if update_values:
        db.execute(
            update(events)
            .where(events.c.id == event_id)
            .values(**update_values)
        )


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


from app.services.governance_votes import compute_plan_vote_summary


def _compute_vote_summary(db: Session, plan_id: UUID, member_count: int) -> dict[str, object]:
    return compute_plan_vote_summary(db, event_plan_votes, plan_id, member_count)


def sync_event_plan_leading_flags(
    db: Session,
    event_id: UUID,
    member_count: int,
) -> UUID | None:
    plan_ids = db.execute(
        select(event_plans.c.id).where(event_plans.c.event_id == event_id)
    ).scalars().all()

    candidates: list[tuple[UUID, float]] = []
    for plan_id in plan_ids:
        summary = _compute_vote_summary(db, plan_id, member_count)
        if summary["is_winning"]:
            candidates.append((plan_id, float(summary["approval_ratio"])))

    db.execute(
        update(event_plans)
        .where(event_plans.c.event_id == event_id)
        .values(is_leading=False)
    )

    leader_id: UUID | None = None
    if candidates:
        max_ratio = max(ratio for _, ratio in candidates)
        top = [plan_id for plan_id, ratio in candidates if ratio == max_ratio]
        if len(top) == 1:
            leader_id = top[0]
            db.execute(
                update(event_plans)
                .where(event_plans.c.id == leader_id)
                .values(is_leading=True, status="approved")
            )
            _sync_event_schedule_from_leading_plan(db, event_id, leader_id)

    return leader_id


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
        if normalized_vote == "neutral":
            if existing_vote is not None:
                db.execute(
                    delete(event_plan_votes).where(
                        event_plan_votes.c.plan_id == plan_id,
                        event_plan_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing_vote is None:
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
        previous_leader = db.execute(
            select(event_plans.c.id).where(
                event_plans.c.event_id == event_row["id"],
                event_plans.c.is_leading.is_(True),
            )
        ).scalar()
        new_leader = sync_event_plan_leading_flags(db, event_row["id"], member_count)
        plan_is_leading = new_leader == plan_id
        leader_changed = new_leader is not None and new_leader != previous_leader

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

    if leader_changed and new_leader is not None:
        leader_author_id = db.execute(
            select(event_plans.c.author_id).where(event_plans.c.id == new_leader)
        ).scalar()
        if leader_author_id is not None:
            create_notification(
                db=db,
                recipient_id=leader_author_id,
                actor_id=current_user_id,
                kind="evt-plan-lead",
                surface="event",
                subject_type="event-plan",
                subject_id=new_leader,
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
        if normalized_vote == "neutral":
            if existing_vote is not None:
                db.execute(
                    delete(event_plan_value_votes).where(
                        event_plan_value_votes.c.plan_id == plan_id,
                        event_plan_value_votes.c.value_id == value_id,
                        event_plan_value_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing_vote is None:
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


def cast_event_plan_criterion_rating(
    db: Session,
    current_user_id: UUID,
    event_slug: str,
    plan_id: UUID,
    criterion_id: str,
    rating: int | None,
) -> dict[str, object]:
    event_row = _get_event_row_by_slug(db, event_slug)
    _ensure_member(db, event_row["id"], current_user_id)

    plan_row = db.execute(
        select(event_plans).where(
            event_plans.c.id == plan_id,
            event_plans.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    value_rows = db.execute(
        select(event_values.c.id, event_values.c.label).where(event_values.c.event_id == event_row["id"])
    ).all()
    prominent_values = [(row[0], row[1]) for row in value_rows]
    allowed_criteria = {
        item["criterionId"]
        for item in assessment_criteria_for_plan(
            plan_kind="event",
            prominent_values=prominent_values,
        )
    }
    normalized_criterion_id = criterion_id.strip()
    if normalized_criterion_id not in allowed_criteria:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown plan criterion")

    value_id = parse_value_criterion_id(normalized_criterion_id)
    if value_id is not None:
        value_row = db.execute(
            select(event_values.c.id).where(
                event_values.c.id == value_id,
                event_values.c.event_id == event_row["id"],
            )
        ).first()
        if value_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event value not found")

    existing_rating = db.execute(
        select(event_plan_criterion_ratings.c.rating).where(
            event_plan_criterion_ratings.c.plan_id == plan_id,
            event_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
            event_plan_criterion_ratings.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if rating is None:
            if existing_rating is not None:
                db.execute(
                    delete(event_plan_criterion_ratings).where(
                        event_plan_criterion_ratings.c.plan_id == plan_id,
                        event_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
                        event_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                )
            normalized_rating = None
        else:
            if rating not in VALID_PLAN_RATINGS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="rating must be between 1 and 5",
                )
            if existing_rating is None:
                db.execute(
                    insert(event_plan_criterion_ratings).values(
                        plan_id=plan_id,
                        criterion_id=normalized_criterion_id,
                        voter_id=current_user_id,
                        rating=rating,
                    )
                )
            else:
                db.execute(
                    update(event_plan_criterion_ratings)
                    .where(
                        event_plan_criterion_ratings.c.plan_id == plan_id,
                        event_plan_criterion_ratings.c.criterion_id == normalized_criterion_id,
                        event_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                    .values(rating=rating)
                )
            normalized_rating = rating

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "event-plan-criterion",
                "target_id": str(plan_id),
                "criterion_id": normalized_criterion_id,
                "rating": normalized_rating,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not cast event plan criterion rating",
        ) from exc

    return {
        "ok": True,
        "event_slug": event_slug,
        "plan_id": plan_id,
        "criterion_id": normalized_criterion_id,
        "rating": normalized_rating,
    }
