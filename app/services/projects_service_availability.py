from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    project_activities,
    project_memberships,
    project_service_availability_rules,
    project_service_requests,
    project_service_slot_holds,
    projects,
    users,
)

WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
AVAILABILITY_HORIZON_WEEKS = 8


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


def _get_project_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


def _ensure_personal_service_creator(project_row: Mapping[str, object], user_id: UUID) -> None:
    if project_row["project_mode"] != "personal-service":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only personal-service projects support availability rules",
        )
    if project_row["author_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the service creator can edit availability",
        )


def _serialize_rule(row: Mapping[str, object]) -> dict[str, object]:
    weekday = int(row["weekday"])
    return {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "weekday": weekday,
        "weekdayLabel": WEEKDAY_LABELS[weekday],
        "startTime": _iso(row["start_time"]),
        "endTime": _iso(row["end_time"]),
        "timezone": row["timezone"],
        "startsOn": _iso(row["starts_on"]),
        "endsOn": _iso(row["ends_on"]),
        "note": row["note"],
    }


def _resolve_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip() or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _overlaps(
    left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime
) -> bool:
    return left_start < right_end and right_start < left_end


def _contained(
    inner_start: datetime, inner_end: datetime, outer_start: datetime, outer_end: datetime
) -> bool:
    return inner_start >= outer_start and inner_end <= outer_end


def list_availability_rules(db: Session, project_id: UUID) -> list[dict[str, object]]:
    rows = (
        db.execute(
            select(project_service_availability_rules)
            .where(project_service_availability_rules.c.project_id == project_id)
            .order_by(
                project_service_availability_rules.c.weekday,
                project_service_availability_rules.c.start_time,
            )
        )
        .mappings()
        .all()
    )
    return [_serialize_rule(row) for row in rows]


def list_slot_holds(db: Session, project_id: UUID) -> list[Mapping[str, object]]:
    return (
        db.execute(
            select(project_service_slot_holds).where(
                project_service_slot_holds.c.project_id == project_id
            )
        )
        .mappings()
        .all()
    )


def expand_availability_slots(
    db: Session,
    *,
    project_id: UUID,
    viewer_is_creator: bool,
    now: datetime | None = None,
    horizon_weeks: int = AVAILABILITY_HORIZON_WEEKS,
) -> list[dict[str, object]]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    rules = (
        db.execute(
            select(project_service_availability_rules).where(
                project_service_availability_rules.c.project_id == project_id
            )
        )
        .mappings()
        .all()
    )
    holds = list_slot_holds(db, project_id)
    activities = (
        db.execute(
            select(project_activities)
            .where(
                project_activities.c.project_id == project_id,
                project_activities.c.ends_at > current,
            )
            .order_by(project_activities.c.scheduled_at.asc())
        )
        .mappings()
        .all()
    )
    accepted_requests = (
        db.execute(
            select(
                project_service_requests.c.id,
                project_service_requests.c.title,
                project_service_requests.c.scheduled_at,
                project_service_requests.c.ends_at,
                users.c.username,
            )
            .select_from(
                project_service_requests.outerjoin(
                    users, users.c.id == project_service_requests.c.requester_id
                )
            )
            .where(
                project_service_requests.c.project_id == project_id,
                project_service_requests.c.status.in_(("accepted", "planned")),
                project_service_requests.c.scheduled_at.is_not(None),
                project_service_requests.c.ends_at.is_not(None),
            )
        )
        .mappings()
        .all()
    )

    slots: list[dict[str, object]] = []

    def bookings_for(start: datetime, end: datetime) -> list[dict[str, object]]:
        items = []
        for request in accepted_requests:
            scheduled_at = request["scheduled_at"]
            ends_at = request["ends_at"]
            if scheduled_at is None or ends_at is None:
                continue
            if _overlaps(start, end, scheduled_at, ends_at):
                items.append(
                    {
                        "requestId": str(request["id"]),
                        "requesterUsername": request.get("username") or "unknown",
                        "title": request["title"],
                    }
                )
        return items

    def hold_covers(start: datetime, end: datetime) -> bool:
        return any(_overlaps(start, end, hold["starts_at"], hold["ends_at"]) for hold in holds)

    for rule in rules:
        zone = _resolve_zone(str(rule["timezone"]))
        start_time: time = rule["start_time"]
        end_time: time = rule["end_time"]
        weekday = int(rule["weekday"])
        starts_on: date | None = rule["starts_on"]
        ends_on: date | None = rule["ends_on"]
        cursor = current.astimezone(zone).date()
        last_day = cursor + timedelta(weeks=horizon_weeks)
        while cursor <= last_day:
            if cursor.weekday() == weekday:
                in_window = (starts_on is None or cursor >= starts_on) and (
                    ends_on is None or cursor <= ends_on
                )
                if in_window:
                    start = datetime.combine(cursor, start_time, tzinfo=zone).astimezone(UTC)
                    end = datetime.combine(cursor, end_time, tzinfo=zone).astimezone(UTC)
                    if end <= start:
                        end = end + timedelta(days=1)
                    if end > current:
                        held = hold_covers(start, end)
                        bookings = bookings_for(start, end) if viewer_is_creator else []
                        if held and not viewer_is_creator:
                            cursor += timedelta(days=1)
                            continue
                        slots.append(
                            {
                                "id": f"rule:{rule['id']}:{cursor.isoformat()}",
                                "source": "rule",
                                "ruleId": str(rule["id"]),
                                "title": rule["note"] or "Available",
                                "startAt": start.isoformat(),
                                "endAt": end.isoformat(),
                                "scheduledAt": start.isoformat(),
                                "held": held,
                                "bookings": bookings,
                                "statusTone": "muted" if held else "green",
                            }
                        )
            cursor += timedelta(days=1)

    for activity in activities:
        linked_request_id = activity.get("linked_request_id")
        start = activity["scheduled_at"]
        end = activity["ends_at"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        held = hold_covers(start, end)
        is_booking = linked_request_id is not None
        if not viewer_is_creator and (is_booking or held):
            continue
        bookings = bookings_for(start, end) if viewer_is_creator else []
        slots.append(
            {
                "id": str(activity["id"]),
                "source": "activity",
                "activityId": str(activity["id"]),
                "title": activity["title"],
                "startAt": start.isoformat(),
                "endAt": end.isoformat(),
                "scheduledAt": start.isoformat(),
                "held": held or is_booking,
                "bookings": bookings,
                "statusTone": "yellow" if is_booking else ("muted" if held else "green"),
            }
        )

    slots.sort(key=lambda item: str(item["startAt"]))
    return slots


def request_fits_open_slot(
    db: Session,
    *,
    project_id: UUID,
    scheduled_at: datetime,
    ends_at: datetime,
) -> bool:
    for slot in expand_availability_slots(db, project_id=project_id, viewer_is_creator=False):
        if slot.get("held"):
            continue
        start = datetime.fromisoformat(str(slot["startAt"]))
        end = datetime.fromisoformat(str(slot["endAt"]))
        if _contained(scheduled_at, ends_at, start, end):
            return True
    return False


def create_availability_rule(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    weekday: int,
    start_time: time,
    end_time: time,
    timezone: str,
    starts_on: date | None,
    ends_on: date | None,
    note: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_personal_service_creator(project_row, current_user_id)
    if weekday < 0 or weekday > 6:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="weekday must be 0-6"
        )
    if end_time == start_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_time must differ from start_time",
        )
    _resolve_zone(timezone)
    try:
        created = (
            db.execute(
                insert(project_service_availability_rules)
                .values(
                    project_id=project_row["id"],
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                    timezone=timezone.strip() or "UTC",
                    starts_on=starts_on,
                    ends_on=ends_on,
                    note=note.strip(),
                )
                .returning(project_service_availability_rules)
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create availability rule",
        ) from exc
    return {"rule": _serialize_rule(created)}


def delete_availability_rule(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    rule_id: UUID,
) -> dict[str, object]:
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_personal_service_creator(project_row, current_user_id)
    deleted = db.execute(
        delete(project_service_availability_rules).where(
            project_service_availability_rules.c.id == rule_id,
            project_service_availability_rules.c.project_id == project_row["id"],
        )
    )
    if deleted.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Availability rule not found"
        )
    db.commit()
    return {"ok": True}


def create_slot_hold(
    db: Session,
    *,
    project_id: UUID,
    request_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    existing = db.execute(
        select(project_service_slot_holds.c.id).where(
            project_service_slot_holds.c.request_id == request_id
        )
    ).first()
    if existing is not None:
        return
    db.execute(
        insert(project_service_slot_holds).values(
            project_id=project_id,
            request_id=request_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    )


def viewer_is_project_member(db: Session, project_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    return row is not None
