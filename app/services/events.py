from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    channels,
    event_attendance,
    event_memberships,
    event_signals,
    event_tags,
    events,
)

EVENT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
EVENT_ATTENDANCE_STATES = frozenset({"going", "not-going"})


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


def _resolve_channel_ids(db: Session, channel_slugs: list[str]) -> list[UUID]:
    normalized = [value.strip().lower() for value in channel_slugs if value.strip()]
    if not normalized:
        return []

    rows = db.execute(
        select(channels.c.id, channels.c.slug).where(channels.c.slug.in_(normalized))
    ).mappings().all()
    found = {row["slug"] for row in rows}
    missing = sorted(set(normalized) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel slugs: {missing}",
        )

    return [row["id"] for row in rows]


def _get_event_tags(db: Session, event_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            event_tags.c.id,
            event_tags.c.tag_kind,
            event_tags.c.channel_id,
            event_tags.c.community_id,
        ).where(event_tags.c.event_id == event_id)
    ).mappings().all()
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
    scheduled_at: datetime | None = None,
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    if not is_private and not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Public events require at least one channel tag",
        )

    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(events)
            .values(
                slug=normalized_slug,
                title=title.strip(),
                description=description.strip(),
                created_by=current_user_id,
                is_private=is_private,
                current_phase_id="phase-1",
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
        ).mappings().one()

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

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Event slug already exists") from exc

    tags = _get_event_tags(db, created["id"])
    return {"event": _serialize_event(created, tags, {"demand": 0, "opposition": 0, "total": 0})}


async def get_event_by_slug(db: Session, cache: Redis, slug: str) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    tags = _get_event_tags(db, row["id"])
    signal_counts = await _get_signal_counts(db, cache, row["id"])
    return {"event": _serialize_event(row, tags, signal_counts)}


def join_event(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)

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
        db.execute(
            update(events)
            .where(events.c.id == event_row["id"])
            .values(member_count=events.c.member_count + 1)
        )
        db.commit()

    return {"ok": True, "joined": True, "slug": event_row["slug"]}


def toggle_event_attendance(
    db: Session,
    current_user_id: UUID,
    slug: str,
    attendance_state: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    normalized_state = attendance_state.strip().lower()

    if normalized_state not in EVENT_ATTENDANCE_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"attendance_state must be one of: {sorted(EVENT_ATTENDANCE_STATES)}",
        )

    existing = db.execute(
        select(event_attendance.c.event_id, event_attendance.c.user_id, event_attendance.c.attendance_state)
        .where(
            event_attendance.c.event_id == event_row["id"],
            event_attendance.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"
    going_count_delta = 0

    try:
        if existing is None:
            db.execute(
                insert(event_attendance).values(
                    event_id=event_row["id"],
                    user_id=current_user_id,
                    attendance_state=normalized_state,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            action = "added"
            if normalized_state == "going":
                going_count_delta = 1
        elif existing["attendance_state"] == normalized_state:
            db.execute(
                delete(event_attendance).where(
                    event_attendance.c.event_id == event_row["id"],
                    event_attendance.c.user_id == current_user_id,
                )
            )
            action = "removed"
            if normalized_state == "going":
                going_count_delta = -1
        else:
            db.execute(
                update(event_attendance)
                .where(
                    event_attendance.c.event_id == event_row["id"],
                    event_attendance.c.user_id == current_user_id,
                )
                .values(attendance_state=normalized_state, updated_at=datetime.now(timezone.utc))
            )
            action = "switched"
            if existing["attendance_state"] == "going" and normalized_state == "not-going":
                going_count_delta = -1
            elif existing["attendance_state"] == "not-going" and normalized_state == "going":
                going_count_delta = 1

        if going_count_delta != 0:
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(going_count=func.greatest(events.c.going_count + going_count_delta, 0))
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not toggle attendance",
        ) from exc

    current = db.execute(
        select(event_attendance.c.attendance_state)
        .where(
            event_attendance.c.event_id == event_row["id"],
            event_attendance.c.user_id == current_user_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    refreshed_going = db.execute(
        select(events.c.going_count).where(events.c.id == event_row["id"])
    ).scalar_one()

    return {
        "ok": True,
        "slug": event_row["slug"],
        "action": action,
        "attendance_state": current,
        "going_count": int(refreshed_going),
    }


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