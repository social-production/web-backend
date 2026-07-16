from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    event_activity_assignments,
    event_activity_roles,
    event_activities,
    event_edit_request_votes,
    event_edit_requests,
    event_editors,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_criterion_ratings,
    event_plan_value_votes,
    event_plan_votes,
    event_plans,
    event_signals,
    event_tags,
    event_update_request_votes,
    event_update_requests,
    event_updates,
    event_value_importance_votes,
    event_values,
    events,
    reports,
    scope_memberships,
    user_follows,
    users,
)
from app.cache import cache_ttl_seconds
from app.services.access_control import assert_can_view_entity
from app.services.activity_history import (
    build_event_history_items,
    ensure_activity_roles_unlocked,
    ensure_future_scheduled_start,
    is_activity_ended,
    load_event_ratings_by_activity,
    utc_now,
)
from app.services.content import activity_status_tone
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document
from app.services.plan_criteria import assessment_criteria_for_plan, serialize_plan_criterion_assessments
from app.utils.votes import is_platform_event, required_votes, resolve_event_vote_population

EVENT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
_PLACEHOLDER_SCHEDULE_LABELS = frozenset({"tbd", "not specified", "to be determined"})
EVENT_PHASES = (
    ("proposal", 1, "P1", "Proposal", "Collect demand and define event values."),
    ("event-plan", 2, "P2", "Event Plan", "Propose and approve event plans."),
    ("activity", 3, "P3", "Activity", "Run event activities."),
    ("closed", 4, "P4", "Closed", "Event is closed."),
)
import importlib
_mod = importlib.import_module('app.services.events.helpers')
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('__') or k == '__all__'})

def join_event(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)

    existing = db.execute(
        select(event_memberships.c.event_id).where(
            event_memberships.c.event_id == event_row["id"],
            event_memberships.c.user_id == current_user_id,
        )
    ).first()
    if existing is not None:
        return {"ok": True, "joined": True, "slug": event_row["slug"]}

    if event_row["is_private"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Private events are invite-only")

    inserted = False
    try:
        db.execute(
            insert(event_memberships).values(
                event_id=event_row["id"],
                user_id=current_user_id,
                role="member",
                joined_at=datetime.now(timezone.utc),
            )
        )
        inserted = True
    except IntegrityError:
        db.rollback()

    if inserted:
        try:
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(member_count=events.c.member_count + 1)
            )
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not join event") from exc

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="join-event",
            metadata={"event_id": str(event_row["id"]), "event_slug": event_row["slug"]},
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not join event") from exc

    return {"ok": True, "joined": True, "slug": event_row["slug"]}


def leave_event(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)

    existing = db.execute(
        select(event_memberships.c.event_id, event_memberships.c.user_id)
        .where(
            event_memberships.c.event_id == event_row["id"],
            event_memberships.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    if existing is None:
        return {"ok": True, "joined": False, "slug": event_row["slug"]}

    try:
        db.execute(
            delete(event_memberships).where(
                event_memberships.c.event_id == event_row["id"],
                event_memberships.c.user_id == current_user_id,
            )
        )
        db.execute(
            update(events)
            .where(events.c.id == event_row["id"])
            .values(member_count=func.greatest(events.c.member_count - 1, 0))
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not leave event",
        ) from exc

    # Invalidate weekly-active caches so quorum drops immediately
    try:
        from app.cache import get_sync_redis_client
        redis = get_sync_redis_client()
        redis.delete(f"governance:weekly_active:event:{event_row['id']}")
        redis.delete("governance:weekly_active")
    except Exception:
        pass

    return {"ok": True, "joined": False, "slug": event_row["slug"]}


async def toggle_event_signal(
    db: Session,
    cache: Redis,
    current_user_id: UUID,
    slug: str,
    signal_type: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    normalized_signal = signal_type.strip().lower()

    if normalized_signal not in EVENT_SIGNAL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"signal_type must be one of: {sorted(EVENT_SIGNAL_TYPES)}",
        )

    existing = db.execute(
        select(event_signals.c.id, event_signals.c.signal_type)
        .where(
            event_signals.c.event_id == event_row["id"],
            event_signals.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"

    try:
        if existing is None:
            db.execute(
                insert(event_signals).values(
                    event_id=event_row["id"],
                    user_id=current_user_id,
                    signal_type=normalized_signal,
                )
            )
            action = "added"
        elif existing["signal_type"] == normalized_signal:
            db.execute(delete(event_signals).where(event_signals.c.id == existing["id"]))
            action = "removed"
        else:
            db.execute(
                update(event_signals)
                .where(event_signals.c.id == existing["id"])
                .values(signal_type=normalized_signal)
            )
            action = "switched"

        if action in {"added", "switched"}:
            record_meaningful_action(
                db=db,
                user_id=current_user_id,
                action_type="signal-demand" if normalized_signal == "demand" else "signal-opposition",
                metadata={"event_id": str(event_row["id"]), "event_slug": event_row["slug"]},
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not toggle signal") from exc

    counts = _get_signal_counts_db(db, event_row["id"])
    await _write_signal_counts_cache(cache, event_row["id"], counts)

    return {
        "ok": True,
        "slug": event_row["slug"],
        "action": action,
        "signal_type": normalized_signal,
        "signals": counts,
    }


def grant_event_editor(
    db: Session,
    current_user_id: UUID,
    slug: str,
    target_user_id: UUID,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    if event_row["created_by"] != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only event creator can manage editors")

    _ensure_event_member(db, event_row["id"], target_user_id)

    try:
        db.execute(
            insert(event_editors).values(
                event_id=event_row["id"],
                user_id=target_user_id,
                granted_by=current_user_id,
                granted_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()

    return {
        "ok": True,
        "slug": event_row["slug"],
        "editor_user_id": target_user_id,
        "granted": True,
    }


def revoke_event_editor(
    db: Session,
    current_user_id: UUID,
    slug: str,
    target_user_id: UUID,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    if event_row["created_by"] != current_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only event creator can manage editors")

    db.execute(
        delete(event_editors).where(
            event_editors.c.event_id == event_row["id"],
            event_editors.c.user_id == target_user_id,
        )
    )
    db.commit()

    return {
        "ok": True,
        "slug": event_row["slug"],
        "editor_user_id": target_user_id,
        "granted": False,
    }


def add_event_value(
    db: Session,
    current_user_id: UUID,
    slug: str,
    label: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    normalized = label.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="label is required")

    try:
        created = db.execute(
            insert(event_values)
            .values(event_id=event_row["id"], label=normalized, author_id=current_user_id)
            .returning(event_values.c.id, event_values.c.event_id, event_values.c.label, event_values.c.author_id, event_values.c.created_at)
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not add event value") from exc

    return {
        "value": {
            "id": created["id"],
            "event_id": created["event_id"],
            "label": created["label"],
            "author_id": created["author_id"],
            "created_at": created["created_at"],
        }
    }


def vote_event_value_importance(
    db: Session,
    current_user_id: UUID,
    slug: str,
    value_id: UUID,
    importance: int,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    if importance < 1 or importance > 10:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="importance must be between 1 and 10")

    value_row = db.execute(
        select(event_values).where(
            event_values.c.id == value_id,
            event_values.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if value_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event value not found")

    existing = db.execute(
        select(event_value_importance_votes.c.importance).where(
            event_value_importance_votes.c.value_id == value_id,
            event_value_importance_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing is None:
            db.execute(
                insert(event_value_importance_votes).values(
                    value_id=value_id,
                    voter_id=current_user_id,
                    importance=importance,
                )
            )
        else:
            db.execute(
                update(event_value_importance_votes)
                .where(
                    event_value_importance_votes.c.value_id == value_id,
                    event_value_importance_votes.c.voter_id == current_user_id,
                )
                .values(importance=importance)
            )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on event value") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "event-value",
            "target_id": str(value_id),
            "event_id": str(event_row["id"]),
            "importance": importance,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on event value") from exc

    return {
        "ok": True,
        "event_slug": event_row["slug"],
        "value_id": value_id,
        "importance": importance,
    }


def create_event_activity(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    scheduled_at: datetime,
    ends_at: datetime,
    location_label: str,
    note: str,
    role_requirements: list[dict[str, object]],
    is_online: bool = False,
    linked_plan_id: UUID | None = None,
    linked_plan_phase_id: str | None = None,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    if ends_at <= scheduled_at:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ends_at must be after scheduled_at")
    ensure_future_scheduled_start(scheduled_at)

    try:
        created = db.execute(
            insert(event_activities)
            .values(
                event_id=event_row["id"],
                linked_plan_id=linked_plan_id,
                linked_plan_phase_id=linked_plan_phase_id,
                title=title.strip(),
                author_id=current_user_id,
                scheduled_at=scheduled_at,
                ends_at=ends_at,
                is_online=is_online,
                location_label=location_label.strip(),
                note=note.strip(),
            )
            .returning(
                event_activities.c.id,
                event_activities.c.event_id,
                event_activities.c.title,
                event_activities.c.author_id,
                event_activities.c.scheduled_at,
                event_activities.c.ends_at,
                event_activities.c.is_online,
                event_activities.c.location_label,
                event_activities.c.note,
                event_activities.c.linked_plan_id,
                event_activities.c.linked_plan_phase_id,
                event_activities.c.created_at,
            )
        ).mappings().one()

        role_items = []
        for req in role_requirements:
            label = str(req.get("label", "")).strip()
            required_count = int(req.get("required_count", 0))
            maximum_count_raw = req.get("maximum_count")
            maximum_count = int(maximum_count_raw) if maximum_count_raw is not None else None
            if not label or required_count < 1:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid role requirement")
            if maximum_count is not None and maximum_count < required_count:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="maximum_count must be >= required_count")

            role = db.execute(
                insert(event_activity_roles)
                .values(
                    activity_id=created["id"],
                    label=label,
                    required_count=required_count,
                    maximum_count=maximum_count,
                )
                .returning(
                    event_activity_roles.c.id,
                    event_activity_roles.c.label,
                    event_activity_roles.c.required_count,
                    event_activity_roles.c.maximum_count,
                )
            ).mappings().one()
            role_items.append(dict(role))

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create event activity") from exc
    except HTTPException:
        db.rollback()
        raise
    return {
        "activity": {
            **dict(created),
            "roles": role_items,
        }
    }


def commit_event_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_label: str | None = None,
    role_id: UUID | None = None,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    activity_row = db.execute(
        select(event_activities).where(
            event_activities.c.id == activity_id,
            event_activities.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    ensure_activity_roles_unlocked(activity_row["ends_at"])

    if role_label is None and role_id is not None:
        role_label = db.execute(
            select(event_activity_roles.c.label).where(
                event_activity_roles.c.id == role_id,
                event_activity_roles.c.activity_id == activity_id,
            )
        ).scalar_one_or_none()
        if role_label is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    if role_label is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="role_label is required")

    role_row = db.execute(
        select(event_activity_roles).where(
            event_activity_roles.c.activity_id == activity_id,
            event_activity_roles.c.label == role_label.strip(),
        )
    ).mappings().first()
    if role_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    existing_assignment = db.execute(
        select(event_activity_assignments.c.role_id)
        .select_from(event_activity_assignments.join(event_activity_roles, event_activity_roles.c.id == event_activity_assignments.c.role_id))
        .where(
            event_activity_roles.c.activity_id == activity_id,
            event_activity_assignments.c.user_id == current_user_id,
        )
    ).first()
    if existing_assignment is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already assigned in this activity")

    filled_count = db.execute(
        select(event_activity_assignments.c.user_id).where(event_activity_assignments.c.role_id == role_row["id"])
    ).all()
    if role_row["maximum_count"] is not None and len(filled_count) >= int(role_row["maximum_count"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role is already full")

    try:
        db.execute(
            insert(event_activity_assignments).values(role_id=role_row["id"], user_id=current_user_id)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not commit event activity role") from exc

    return {"ok": True, "event_slug": event_row["slug"], "activity_id": activity_id, "role_id": role_row["id"], "user_id": current_user_id}


def uncommit_event_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    activity_row = db.execute(
        select(event_activities).where(
            event_activities.c.id == activity_id,
            event_activities.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    ensure_activity_roles_unlocked(activity_row["ends_at"])

    role_ids = db.execute(
        select(event_activity_roles.c.id).where(event_activity_roles.c.activity_id == activity_id)
    ).scalars().all()

    if role_ids:
        db.execute(
            delete(event_activity_assignments).where(
                event_activity_assignments.c.role_id.in_(role_ids),
                event_activity_assignments.c.user_id == current_user_id,
            )
        )
        db.commit()

    return {"ok": True, "event_slug": event_row["slug"], "activity_id": activity_id}


def share_event_with_user(
    db: Session,
    current_user_id: UUID,
    slug: str,
    username: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    _ensure_event_member(db, event_row["id"], current_user_id)

    normalized_username = username.strip()
    if not normalized_username:
        return {"ok": False, "error": "Choose another user."}

    target_user = db.execute(
        select(users.c.id, users.c.username).where(users.c.username == normalized_username)
    ).mappings().first()
    if target_user is None or target_user["id"] == current_user_id:
        return {"ok": False, "error": "Choose another user."}

    if event_row["is_private"]:
        existing_membership = db.execute(
            select(event_memberships.c.event_id).where(
                event_memberships.c.event_id == event_row["id"],
                event_memberships.c.user_id == target_user["id"],
            )
        ).first()
        if existing_membership is None:
            db.execute(
                insert(event_memberships).values(
                    event_id=event_row["id"],
                    user_id=target_user["id"],
                    role="member",
                    joined_at=datetime.now(timezone.utc),
                )
            )
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(member_count=events.c.member_count + 1)
            )

    create_notification(
        db=db,
        recipient_id=target_user["id"],
        actor_id=current_user_id,
        kind="evt-share",
        surface="event",
        subject_type="event",
        subject_id=event_row["id"],
        target_id=event_row["id"],
        title=event_row["title"],
        body=f"An event was shared with you: {event_row['title']}. Open /events/{event_row['slug']}",
        href=f"/events/{event_row['slug']}",
    )
    return {"ok": True}