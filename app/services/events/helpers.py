from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cache import cache_ttl_seconds
from app.models import (
    channels,
    communities,
    event_memberships,
    event_signals,
    event_tags,
    events,
    scope_memberships,
    users,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.search import index_document
from app.utils.votes import required_votes

EVENT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
_PLACEHOLDER_SCHEDULE_LABELS = frozenset({"tbd", "not specified", "to be determined"})
EVENT_PHASES = (
    ("proposal", 1, "P1", "Proposal", "Collect demand and define event values."),
    ("event-plan", 2, "P2", "Event Plan", "Propose and approve event plans."),
    ("activity", 3, "P3", "Activity", "Run event activities."),
    ("closed", 4, "P4", "Closed", "Event is closed."),
)


def _is_meaningful_schedule_label(label: str | None) -> bool:
    normalized = (label or "").strip()
    return bool(normalized) and normalized.lower() not in _PLACEHOLDER_SCHEDULE_LABELS


def _event_search_meta(time_label: str | None, location_label: str | None) -> str:
    location = (location_label or "").strip()
    if _is_meaningful_schedule_label(location):
        return location

    time = (time_label or "").strip()
    if _is_meaningful_schedule_label(time):
        return time

    return "Event"


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return datetime.now(UTC).isoformat()


def _parse_plan_local_datetime(
    date_value: str | None, time_value: str | None, fallback_time: str
) -> datetime | None:
    normalized_date = (date_value or "").strip()
    if not normalized_date:
        return None

    normalized_time = (time_value or "").strip() or fallback_time
    if len(normalized_time) != 5 or ":" not in normalized_time:
        normalized_time = fallback_time

    try:
        parsed = datetime.fromisoformat(f"{normalized_date}T{normalized_time}")
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _event_plan_schedule_bounds(schedule: dict | None) -> tuple[datetime | None, datetime | None]:
    if not schedule:
        return None, None

    start_date = schedule.get("startDate") or ""
    end_date = schedule.get("endDate") or start_date
    start = _parse_plan_local_datetime(start_date, schedule.get("startTimeLabel"), "00:00")
    end = _parse_plan_local_datetime(end_date, schedule.get("finishTimeLabel"), "23:59")
    return start, end


def _iter_plan_day_isos(schedule: dict | None) -> list[str]:
    if not schedule:
        return []

    start_date = (schedule.get("startDate") or "").strip()
    if not start_date:
        return []

    end_date = (schedule.get("endDate") or start_date).strip()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return [start_date]

    if end < start:
        return [start_date]

    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _is_future_selectable_plan_day(schedule: dict | None, iso_day: str, now: datetime) -> bool:
    today = now.date().isoformat()
    if iso_day > today:
        return True
    if iso_day < today:
        return False

    _, schedule_end = _event_plan_schedule_bounds(schedule)
    return schedule_end is not None and schedule_end > now


def _can_propose_event_activity(schedule: dict | None, now: datetime | None = None) -> bool:
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    _, schedule_end = _event_plan_schedule_bounds(schedule)
    if schedule_end is None or schedule_end <= reference:
        return False

    return any(
        _is_future_selectable_plan_day(schedule, iso_day, reference)
        for iso_day in _iter_plan_day_isos(schedule)
    )


def _username_lookup(db: Session, user_ids: set[UUID]) -> dict[UUID, dict[str, str]]:
    if not user_ids:
        return {}

    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
            users.c.id.in_(list(user_ids))
        )
    ).all()
    return {
        row[0]: {
            "username": row[1],
            "bio": row[2] or "",
            "profileImageUrl": row[3],
        }
        for row in rows
    }


def _vote_summary(
    vote_rows: list[tuple[str, UUID]],
    member_count: int,
    current_user_id: UUID | None,
) -> tuple[dict[str, object], bool, bool]:
    yes_count = 0
    no_count = 0
    active_vote: str | None = None
    for vote, voter_id in vote_rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1
        if current_user_id is not None and voter_id == current_user_id:
            active_vote = vote

    total_votes = yes_count + no_count
    votes_required = required_votes(member_count)
    approval_percent = (yes_count / total_votes * 100.0) if total_votes > 0 else 0.0
    meets_quorum = total_votes >= votes_required
    passes = meets_quorum and approval_percent >= 66.0

    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_meet_quorum = max_total >= votes_required
    can_meet_approval = (max_yes / max_total * 100.0) >= 66.0 if max_total > 0 else False
    can_still_pass = (not passes) and can_meet_quorum and can_meet_approval

    quorum_threshold_percent = (votes_required / member_count * 100.0) if member_count > 0 else 0.0
    summary = {
        "yesCount": yes_count,
        "noCount": no_count,
        "totalVotes": total_votes,
        "approvalPercent": approval_percent,
        "activeVote": active_vote,
        "meetsQuorum": meets_quorum,
        "eligibleVoterCount": member_count,
        "quorumThresholdPercent": quorum_threshold_percent,
        "votesRequired": votes_required,
        "votesRemaining": max(0, votes_required - total_votes),
        "remainingEligibleVotes": remaining_eligible,
    }
    return summary, passes, can_still_pass


def _plan_leader_status(
    *,
    is_leading: bool,
    passes: bool,
    approval_percent: float,
    passing_plans: list[tuple[str, float]],
) -> str | None:
    if is_leading:
        return "leading"
    if not passes or not passing_plans:
        return None
    max_percent = max(percent for _, percent in passing_plans)
    top_count = sum(1 for _, percent in passing_plans if percent == max_percent)
    if approval_percent == max_percent and top_count > 1:
        return "tied"
    return None


def _event_lifecycle_phases(current_phase_id: str) -> list[dict[str, object]]:
    from app.services.lifecycle_copy import event_phase_copy

    phase_order = {phase_id: order for phase_id, order, _, _, _ in EVENT_PHASES}
    current_order = phase_order.get(current_phase_id, 1)
    phases: list[dict[str, object]] = []
    for phase_id, order, short_label, title, summary in EVENT_PHASES:
        if order < current_order:
            progress = "complete"
        elif order == current_order:
            progress = "current"
        else:
            progress = "upcoming"
        copy = event_phase_copy(phase_id, summary)
        phases.append(
            {
                "id": phase_id,
                "order": order,
                "shortLabel": short_label,
                "title": title,
                "summary": copy["summary"],
                "progressState": progress,
                "eventStatus": "active",
                "mechanics": copy["mechanics"],
                "note": copy["note"],
            }
        )
    return phases


def _serialize_event(
    row: Mapping[str, object],
    tags: list[dict[str, object]],
    signal_counts: dict[str, int],
) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"],
        "created_by": row["created_by"],
        "is_private": row["is_private"],
        "current_phase_id": row["current_phase_id"],
        "time_label": row["time_label"],
        "location_label": row["location_label"],
        "scheduled_at": row["scheduled_at"],
        "signal_count": signal_counts["total"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "going_count": row["going_count"],
        "member_count": row["member_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"],
        "tags": tags,
        "signals": signal_counts,
    }


def _get_event_by_slug_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return row


def _ensure_event_member(db: Session, event_id: UUID, user_id: UUID) -> None:
    member = db.execute(
        select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == event_id,
            event_memberships.c.user_id == user_id,
        )
    ).first()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only event members can perform this action",
        )


def _resolve_channel_ids(db: Session, channel_slugs: list[str]) -> list[UUID]:
    normalized = [value.strip().lower() for value in channel_slugs if value.strip()]
    if not normalized:
        return []

    rows = (
        db.execute(select(channels.c.id, channels.c.slug).where(channels.c.slug.in_(normalized)))
        .mappings()
        .all()
    )
    found = {row["slug"] for row in rows}
    missing = sorted(set(normalized) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel slugs: {missing}",
        )

    return [row["id"] for row in rows]


def _resolve_community_ids(
    db: Session, community_slugs: list[str], current_user_id: UUID
) -> list[UUID]:
    normalized = [value.strip().lower() for value in community_slugs if value.strip()]
    if not normalized:
        return []

    rows = (
        db.execute(
            select(communities.c.id, communities.c.slug, communities.c.join_policy).where(
                communities.c.slug.in_(normalized)
            )
        )
        .mappings()
        .all()
    )
    found = {row["slug"] for row in rows}
    missing = sorted(set(normalized) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown community slugs: {missing}",
        )

    closed_ids = [row["id"] for row in rows if row["join_policy"] == "closed"]
    if closed_ids:
        membership_rows = db.execute(
            select(scope_memberships.c.scope_id).where(
                scope_memberships.c.scope_kind == "community",
                scope_memberships.c.scope_id.in_(closed_ids),
                scope_memberships.c.user_id == current_user_id,
            )
        ).all()
        member_ids = {row[0] for row in membership_rows}
        forbidden = sorted(
            row["slug"] for row in rows if row["id"] in closed_ids and row["id"] not in member_ids
        )
        if forbidden:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You must be a member to tag private communities: {forbidden}",
            )

    return [row["id"] for row in rows]


def _get_event_tags(db: Session, event_id: UUID) -> list[dict[str, object]]:
    rows = (
        db.execute(
            select(
                event_tags.c.id,
                event_tags.c.tag_kind,
                event_tags.c.channel_id,
                event_tags.c.community_id,
            ).where(event_tags.c.event_id == event_id)
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _get_signal_counts_db(db: Session, event_id: UUID) -> dict[str, int]:
    grouped_rows = db.execute(
        select(event_signals.c.signal_type, func.count().label("count"))
        .where(event_signals.c.event_id == event_id)
        .group_by(event_signals.c.signal_type)
    ).all()

    demand = 0
    opposition = 0
    for signal_type, count in grouped_rows:
        if signal_type == "demand":
            demand = int(count)
        elif signal_type == "opposition":
            opposition = int(count)

    return {
        "demand": demand,
        "opposition": opposition,
        "total": demand + opposition,
    }


async def _write_signal_counts_cache(cache: Redis, event_id: UUID, counts: dict[str, int]) -> None:
    key = f"event:{event_id}:signals"
    await cache.hset(
        key,
        mapping={
            "demand": str(counts["demand"]),
            "opposition": str(counts["opposition"]),
            "total": str(counts["total"]),
        },
    )
    await cache.expire(key, cache_ttl_seconds())


async def _get_signal_counts(db: Session, cache: Redis, event_id: UUID) -> dict[str, int]:
    key = f"event:{event_id}:signals"
    cached = await cache.hgetall(key)
    if cached:
        return {
            "demand": int(cached.get("demand", 0)),
            "opposition": int(cached.get("opposition", 0)),
            "total": int(cached.get("total", 0)),
        }

    counts = _get_signal_counts_db(db, event_id)
    await _write_signal_counts_cache(cache, event_id, counts)
    return counts


def create_event(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    description: str,
    is_private: bool,
    time_label: str,
    location_label: str,
    channel_slugs: list[str],
    community_slugs: list[str] | None = None,
    scheduled_at: datetime | None = None,
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required"
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    community_ids = _resolve_community_ids(db, community_slugs or [], current_user_id)
    if not is_private and not channel_ids and not community_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Public events require at least one channel or community tag",
        )

    now = datetime.now(UTC)

    try:
        created = (
            db.execute(
                insert(events)
                .values(
                    slug=normalized_slug,
                    title=title.strip(),
                    description=description.strip(),
                    created_by=current_user_id,
                    is_private=is_private,
                    current_phase_id="proposal",
                    time_label=time_label.strip(),
                    location_label=location_label.strip(),
                    scheduled_at=scheduled_at,
                    member_count=1,
                    last_activity_at=now,
                )
                .returning(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.description,
                    events.c.created_by,
                    events.c.is_private,
                    events.c.current_phase_id,
                    events.c.time_label,
                    events.c.location_label,
                    events.c.scheduled_at,
                    events.c.vote_count,
                    events.c.comment_count,
                    events.c.going_count,
                    events.c.member_count,
                    events.c.created_at,
                    events.c.updated_at,
                    events.c.last_activity_at,
                )
            )
            .mappings()
            .one()
        )

        db.execute(
            insert(event_memberships).values(
                event_id=created["id"],
                user_id=current_user_id,
                role="member",
                joined_at=now,
            )
        )

        for channel_id in channel_ids:
            db.execute(
                insert(event_tags).values(
                    event_id=created["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        for community_id in community_ids:
            db.execute(
                insert(event_tags).values(
                    event_id=created["id"],
                    tag_kind="community",
                    channel_id=None,
                    community_id=community_id,
                )
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-event",
            metadata={"event_id": str(created["id"]), "slug": created["slug"]},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Event slug already exists"
        ) from exc

    tags = _get_event_tags(db, created["id"])
    index_document(
        db=db,
        entity_type="event",
        entity_id=created["id"],
        title=created["title"],
        summary=created["description"],
        meta=_event_search_meta(created["time_label"], created["location_label"]),
        href=f"/events/{created['slug']}",
    )
    return {"event": _serialize_event(created, tags, {"demand": 0, "opposition": 0, "total": 0})}


async def get_event_by_slug(db: Session, cache: Redis, slug: str) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    tags = _get_event_tags(db, row["id"])
    signal_counts = await _get_signal_counts(db, cache, row["id"])
    return {"event": _serialize_event(row, tags, signal_counts)}
