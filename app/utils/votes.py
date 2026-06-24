from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from math import ceil, log10
from uuid import UUID

from redis import Redis as SyncRedis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cache import get_sync_redis_client
from app.models import channels, event_memberships, event_tags, meaningful_actions, project_memberships

WEEKLY_ACTIVE_CACHE_KEY = "governance:weekly_active"
WEEKLY_ACTIVE_CACHE_TTL_SECONDS = 300
PLATFORM_CHANNEL_SLUG = "platform"


def required_votes(n: int) -> int:
    if n <= 0:
        return 0

    if n < 100:
        error_margin = 0.10 - (0.03 * (n - 1) / 99)
    elif n < 500:
        error_margin = 0.07 - (0.02 * (n - 100) / 400)
    else:
        error_margin = max(0.02, 0.05 - 0.03 * log10(n / 500) / log10(2000))

    base_sample_size = 0.9604 / (error_margin ** 2)
    cochran = ceil(base_sample_size / (1 + (base_sample_size - 1) / n))
    return min(ceil(0.75 * n), cochran)


@lru_cache(maxsize=1)
def _redis_client() -> SyncRedis:
    return get_sync_redis_client()


def _week_ago() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=7)


def weekly_active_users_global(db: Session) -> int:
    try:
        cached = _redis_client().get(WEEKLY_ACTIVE_CACHE_KEY)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        pass

    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct())).where(
            meaningful_actions.c.occurred_at >= _week_ago()
        )
    ).scalar_one()
    computed = int(total or 0)

    try:
        _redis_client().setex(WEEKLY_ACTIVE_CACHE_KEY, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
    except Exception:
        pass

    return computed


def weekly_active_project_members(db: Session, project_id: UUID) -> int:
    cache_key = f"governance:weekly_active:project:{project_id}"
    try:
        cached = _redis_client().get(cache_key)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        pass

    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct()))
        .select_from(
            meaningful_actions.join(
                project_memberships,
                meaningful_actions.c.user_id == project_memberships.c.user_id,
            )
        )
        .where(
            project_memberships.c.project_id == project_id,
            meaningful_actions.c.occurred_at >= _week_ago(),
        )
    ).scalar_one()
    computed = int(total or 0)

    try:
        _redis_client().setex(cache_key, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
    except Exception:
        pass

    return computed


def weekly_active_event_members(db: Session, event_id: UUID) -> int:
    cache_key = f"governance:weekly_active:event:{event_id}"
    try:
        cached = _redis_client().get(cache_key)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        pass

    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct()))
        .select_from(
            meaningful_actions.join(
                event_memberships,
                meaningful_actions.c.user_id == event_memberships.c.user_id,
            )
        )
        .where(
            event_memberships.c.event_id == event_id,
            meaningful_actions.c.occurred_at >= _week_ago(),
        )
    ).scalar_one()
    computed = int(total or 0)

    try:
        _redis_client().setex(cache_key, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
    except Exception:
        pass

    return computed


def is_platform_event(db: Session, event_id: UUID) -> bool:
    row = db.execute(
        select(event_tags.c.id)
        .select_from(event_tags.join(channels, event_tags.c.channel_id == channels.c.id))
        .where(
            event_tags.c.event_id == event_id,
            event_tags.c.tag_kind == "channel",
            channels.c.slug == PLATFORM_CHANNEL_SLUG,
        )
        .limit(1)
    ).first()
    return row is not None


def resolve_project_vote_population(
    db: Session,
    project_id: UUID,
    is_platform_tagged: bool,
) -> int:
    if is_platform_tagged:
        return weekly_active_users_global(db)
    return weekly_active_project_members(db, project_id)


def resolve_event_vote_population(db: Session, event_id: UUID) -> int:
    if is_platform_event(db, event_id):
        return weekly_active_users_global(db)
    return weekly_active_event_members(db, event_id)
