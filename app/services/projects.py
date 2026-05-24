from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import channels, project_memberships, project_signals, project_tags, projects
from app.services.search import index_document

PROJECT_MODES = frozenset({"productive", "collective-service", "personal-service"})
PROJECT_SUBTYPES = frozenset({"standard", "software"})
PROJECT_SIGNAL_TYPES = frozenset({"demand", "opposition"})


def _serialize_project(row: Mapping[str, object], tags: list[dict[str, object]], signal_counts: dict[str, int]) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "project_mode": row["project_mode"],
        "project_subtype": row["project_subtype"],
        "current_phase_id": row["current_phase_id"],
        "stage_label": row["stage_label"],
        "location_label": row["location_label"],
        "is_platform_tagged": row["is_platform_tagged"],
        "is_closed": row["is_closed"],
        "close_outcome": row["close_outcome"],
        "signal_count": row["signal_count"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "member_count": row["member_count"],
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": tags,
        "signals": signal_counts,
    }


def _get_project_by_slug_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
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


def _get_project_tags(db: Session, project_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            project_tags.c.id,
            project_tags.c.tag_kind,
            project_tags.c.channel_id,
            project_tags.c.community_id,
        ).where(project_tags.c.project_id == project_id)
    ).mappings().all()
    return [dict(row) for row in rows]


def _get_signal_counts_db(db: Session, project_id: UUID) -> dict[str, int]:
    grouped_rows = db.execute(
        select(project_signals.c.signal_type, func.count().label("count"))
        .where(project_signals.c.project_id == project_id)
        .group_by(project_signals.c.signal_type)
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


async def _write_signal_counts_cache(cache: Redis, project_id: UUID, counts: dict[str, int]) -> None:
    key = f"project:{project_id}:signals"
    await cache.hset(
        key,
        mapping={
            "demand": str(counts["demand"]),
            "opposition": str(counts["opposition"]),
            "total": str(counts["total"]),
        },
    )


async def _get_signal_counts(db: Session, cache: Redis, project_id: UUID) -> dict[str, int]:
    key = f"project:{project_id}:signals"
    cached = await cache.hgetall(key)
    if cached:
        return {
            "demand": int(cached.get("demand", 0)),
            "opposition": int(cached.get("opposition", 0)),
            "total": int(cached.get("total", 0)),
        }

    counts = _get_signal_counts_db(db, project_id)
    await _write_signal_counts_cache(cache, project_id, counts)
    return counts


def _phase_for_mode(project_mode: str) -> tuple[str, str]:
    return "phase-1", "proposal"


def create_project(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    description: str,
    project_mode: str,
    project_subtype: str | None,
    location_label: str,
    channel_slugs: list[str],
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    normalized_mode = project_mode.strip().lower()
    normalized_subtype = project_subtype.strip().lower() if project_subtype else None

    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")
    if normalized_mode not in PROJECT_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"project_mode must be one of: {sorted(PROJECT_MODES)}",
        )

    if normalized_mode == "personal-service":
        if normalized_subtype is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="personal-service projects must not include project_subtype",
            )
    elif normalized_subtype not in PROJECT_SUBTYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"project_subtype must be one of: {sorted(PROJECT_SUBTYPES)} for non personal-service modes",
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    if not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one channel tag is required",
        )

    phase_id, stage_label = _phase_for_mode(normalized_mode)
    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(projects)
            .values(
                slug=normalized_slug,
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                project_mode=normalized_mode,
                project_subtype=normalized_subtype,
                current_phase_id=phase_id,
                stage_label=stage_label,
                location_label=location_label.strip(),
                member_count=1,
                last_activity_at=now,
            )
            .returning(
                projects.c.id,
                projects.c.slug,
                projects.c.title,
                projects.c.description,
                projects.c.author_id,
                projects.c.project_mode,
                projects.c.project_subtype,
                projects.c.current_phase_id,
                projects.c.stage_label,
                projects.c.location_label,
                projects.c.is_platform_tagged,
                projects.c.is_closed,
                projects.c.close_outcome,
                projects.c.signal_count,
                projects.c.vote_count,
                projects.c.comment_count,
                projects.c.member_count,
                projects.c.last_activity_at,
                projects.c.created_at,
                projects.c.updated_at,
            )
        ).mappings().one()

        db.execute(
            insert(project_memberships).values(
                project_id=created["id"],
                user_id=current_user_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
            )
        )

        for channel_id in channel_ids:
            db.execute(
                insert(project_tags).values(
                    project_id=created["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project slug already exists") from exc

    tags = _get_project_tags(db, created["id"])
    index_document(
        db=db,
        entity_type="project",
        entity_id=created["id"],
        title=created["title"],
        summary=created["description"],
        meta=created["project_mode"],
        href=f"/projects/{created['slug']}",
    )
    return {"project": _serialize_project(created, tags, {"demand": 0, "opposition": 0, "total": 0})}


async def get_project_by_slug(db: Session, cache: Redis, slug: str) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    tags = _get_project_tags(db, row["id"])
    signal_counts = await _get_signal_counts(db, cache, row["id"])
    return {"project": _serialize_project(row, tags, signal_counts)}


def join_project(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)

    inserted = False
    try:
        db.execute(
            insert(project_memberships).values(
                project_id=project_row["id"],
                user_id=current_user_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=datetime.now(timezone.utc),
            )
        )
        inserted = True
    except IntegrityError:
        db.rollback()

    if inserted:
        db.execute(
            update(projects)
            .where(projects.c.id == project_row["id"])
            .values(member_count=projects.c.member_count + 1)
        )
        db.commit()

    return {"ok": True, "joined": True, "slug": project_row["slug"]}


def leave_project(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)

    result = db.execute(
        delete(project_memberships).where(
            project_memberships.c.project_id == project_row["id"],
            project_memberships.c.user_id == current_user_id,
        )
    )

    if result.rowcount and result.rowcount > 0:
        db.execute(
            update(projects)
            .where(projects.c.id == project_row["id"])
            .values(member_count=func.greatest(projects.c.member_count - 1, 0))
        )

    db.commit()
    return {"ok": True, "joined": False, "slug": project_row["slug"]}


async def toggle_project_signal(
    db: Session,
    cache: Redis,
    current_user_id: UUID,
    slug: str,
    signal_type: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    normalized_signal = signal_type.strip().lower()

    if normalized_signal not in PROJECT_SIGNAL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"signal_type must be one of: {sorted(PROJECT_SIGNAL_TYPES)}",
        )

    existing = db.execute(
        select(project_signals.c.id, project_signals.c.signal_type)
        .where(
            project_signals.c.project_id == project_row["id"],
            project_signals.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"
    signal_count_delta = 0

    try:
        if existing is None:
            db.execute(
                insert(project_signals).values(
                    project_id=project_row["id"],
                    user_id=current_user_id,
                    signal_type=normalized_signal,
                )
            )
            signal_count_delta = 1
            action = "added"
        elif existing["signal_type"] == normalized_signal:
            db.execute(delete(project_signals).where(project_signals.c.id == existing["id"]))
            signal_count_delta = -1
            action = "removed"
        else:
            db.execute(
                update(project_signals)
                .where(project_signals.c.id == existing["id"])
                .values(signal_type=normalized_signal)
            )
            action = "switched"

        if signal_count_delta != 0:
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(signal_count=func.greatest(projects.c.signal_count + signal_count_delta, 0))
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not toggle signal") from exc

    counts = _get_signal_counts_db(db, project_row["id"])
    await _write_signal_counts_cache(cache, project_row["id"], counts)

    return {
        "ok": True,
        "slug": project_row["slug"],
        "action": action,
        "signal_type": normalized_signal,
        "signals": counts,
    }
