from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from math import ceil, log10
from uuid import UUID

from fastapi import HTTPException, status
from redis import Redis as SyncRedis
from sqlalchemy import false as sql_false
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cache import get_sync_redis_client
from app.config import get_settings
from app.models import (
    channels,
    event_memberships,
    event_tags,
    meaningful_actions,
    project_memberships,
    users,
)

CONTENT_REMOVED_ACTION = "content-removed"
WARMUP_DETAIL = (
    "New accounts can take part in votes after a short warm-up. "
    "Join in and contribute a little, then try again soon."
)

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

    base_sample_size = 0.9604 / (error_margin**2)
    cochran = ceil(base_sample_size / (1 + (base_sample_size - 1) / n))
    return min(ceil(0.75 * n), cochran)


@lru_cache(maxsize=1)
def _redis_client() -> SyncRedis:
    return get_sync_redis_client()


def _week_ago() -> datetime:
    return datetime.now(UTC) - timedelta(days=7)


def governance_guardrails_enabled() -> bool:
    settings = get_settings()
    return (
        settings.governance_min_account_age_hours > 0
        or settings.governance_min_meaningful_actions > 0
    )


def meaningful_action_types() -> frozenset[str]:
    raw = get_settings().governance_meaningful_action_types
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _weekly_active_cache_key(base: str) -> str:
    if not governance_guardrails_enabled():
        return base
    settings = get_settings()
    fingerprint = (
        f"{settings.governance_min_account_age_hours}:"
        f"{settings.governance_min_meaningful_actions}:"
        f"{settings.governance_removal_penalty_hours}:"
        f"{settings.governance_meaningful_action_types}"
    )
    return f"{base}:established:{fingerprint}"


def _effective_start_expr():
    settings = get_settings()
    if settings.governance_removal_penalty_hours <= 0:
        return users.c.created_at
    latest_penalty = (
        select(func.max(meaningful_actions.c.occurred_at))
        .where(
            meaningful_actions.c.user_id == users.c.id,
            meaningful_actions.c.action_type == CONTENT_REMOVED_ACTION,
        )
        .correlate(users)
        .scalar_subquery()
    )
    shifted = latest_penalty + timedelta(hours=settings.governance_removal_penalty_hours)
    return func.greatest(users.c.created_at, func.coalesce(shifted, users.c.created_at))


def _counted_actions_expr():
    return (
        select(func.count())
        .select_from(meaningful_actions)
        .where(
            meaningful_actions.c.user_id == users.c.id,
            meaningful_actions.c.action_type.in_(meaningful_action_types()),
        )
        .correlate(users)
        .scalar_subquery()
    )


def _established_user_id_select():
    """Users who meet the warm-up. None when guardrails are off."""
    if not governance_guardrails_enabled():
        return None
    settings = get_settings()
    conditions = []
    now = datetime.now(UTC)
    age_or_penalty = (
        settings.governance_min_account_age_hours > 0
        or settings.governance_removal_penalty_hours > 0
    )
    if age_or_penalty:
        cutoff = now - timedelta(hours=max(settings.governance_min_account_age_hours, 0))
        conditions.append(_effective_start_expr() <= cutoff)
    if settings.governance_min_meaningful_actions > 0:
        conditions.append(_counted_actions_expr() >= settings.governance_min_meaningful_actions)
    if not conditions:
        return None
    return select(users.c.id).where(*conditions)


def _established_clause(user_id_column):
    established = _established_user_id_select()
    if established is None:
        return None
    return user_id_column.in_(established)


def _layer2_allows(db: Session, user_id: UUID) -> bool:
    """True when warm-up is off, or the account is old enough and active enough."""
    if not governance_guardrails_enabled():
        return True

    settings = get_settings()
    created_at = db.execute(
        select(users.c.created_at).where(users.c.id == user_id)
    ).scalar_one_or_none()
    if created_at is None:
        return False

    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    effective_start = created_at
    if settings.governance_removal_penalty_hours > 0:
        latest_penalty = db.execute(
            select(func.max(meaningful_actions.c.occurred_at)).where(
                meaningful_actions.c.user_id == user_id,
                meaningful_actions.c.action_type == CONTENT_REMOVED_ACTION,
            )
        ).scalar_one()
        if latest_penalty is not None:
            if latest_penalty.tzinfo is None:
                latest_penalty = latest_penalty.replace(tzinfo=UTC)
            penalty_start = latest_penalty + timedelta(
                hours=settings.governance_removal_penalty_hours
            )
            if penalty_start > effective_start:
                effective_start = penalty_start

    min_age = timedelta(hours=max(settings.governance_min_account_age_hours, 0))
    if now - effective_start < min_age:
        return False

    if settings.governance_min_meaningful_actions > 0:
        counted = db.execute(
            select(func.count())
            .select_from(meaningful_actions)
            .where(
                meaningful_actions.c.user_id == user_id,
                meaningful_actions.c.action_type.in_(meaningful_action_types()),
            )
        ).scalar_one()
        if int(counted or 0) < settings.governance_min_meaningful_actions:
            return False
    return True


def is_established_voter(db: Session, user_id: UUID) -> bool:
    """Layer 2, and when the ratio switch is on, a licensed effective ratio."""
    if not _layer2_allows(db, user_id):
        return False
    if not get_settings().governance_trust_ratio_enabled:
        return True
    from app.services.trust import user_can_vote

    return user_can_vote(db, user_id)


def ensure_established_voter(db: Session, user_id: UUID) -> None:
    if not _layer2_allows(db, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WARMUP_DETAIL)
    if not get_settings().governance_trust_ratio_enabled:
        return
    from app.services.trust import trust_vote_detail, user_can_vote

    if user_can_vote(db, user_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=trust_vote_detail())


def _append_voter_filters(db: Session, filters: list, user_id_column) -> None:
    clause = _established_clause(user_id_column)
    if clause is not None:
        filters.append(clause)
    if not get_settings().governance_trust_ratio_enabled:
        return
    from app.services.trust import voting_user_ids

    voter_ids = voting_user_ids(db)
    if voter_ids:
        filters.append(user_id_column.in_(voter_ids))
    else:
        filters.append(sql_false())


def weekly_active_users_global(db: Session) -> int:
    trust_on = get_settings().governance_trust_ratio_enabled
    cache_key = _weekly_active_cache_key(WEEKLY_ACTIVE_CACHE_KEY)
    if not trust_on:
        try:
            cached = _redis_client().get(cache_key)
            if cached is not None:
                return max(0, int(cached))
        except Exception:
            pass

    filters = [meaningful_actions.c.occurred_at >= _week_ago()]
    _append_voter_filters(db, filters, meaningful_actions.c.user_id)
    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct())).where(*filters)
    ).scalar_one()
    computed = int(total or 0)

    if not trust_on:
        try:
            _redis_client().setex(cache_key, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
        except Exception:
            pass

    return computed


def weekly_active_project_members(db: Session, project_id: UUID) -> int:
    trust_on = get_settings().governance_trust_ratio_enabled
    cache_key = _weekly_active_cache_key(f"governance:weekly_active:project:{project_id}")
    if not trust_on:
        try:
            cached = _redis_client().get(cache_key)
            if cached is not None:
                return max(0, int(cached))
        except Exception:
            pass

    project_filters = [
        project_memberships.c.project_id == project_id,
        meaningful_actions.c.occurred_at >= _week_ago(),
    ]
    _append_voter_filters(db, project_filters, meaningful_actions.c.user_id)
    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct()))
        .select_from(
            meaningful_actions.join(
                project_memberships,
                meaningful_actions.c.user_id == project_memberships.c.user_id,
            )
        )
        .where(*project_filters)
    ).scalar_one()
    computed = int(total or 0)

    if not trust_on:
        try:
            _redis_client().setex(cache_key, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
        except Exception:
            pass

    return computed


def weekly_active_event_members(db: Session, event_id: UUID) -> int:
    trust_on = get_settings().governance_trust_ratio_enabled
    cache_key = _weekly_active_cache_key(f"governance:weekly_active:event:{event_id}")
    if not trust_on:
        try:
            cached = _redis_client().get(cache_key)
            if cached is not None:
                return max(0, int(cached))
        except Exception:
            pass

    event_filters = [
        event_memberships.c.event_id == event_id,
        meaningful_actions.c.occurred_at >= _week_ago(),
    ]
    _append_voter_filters(db, event_filters, meaningful_actions.c.user_id)
    total = db.execute(
        select(func.count(meaningful_actions.c.user_id.distinct()))
        .select_from(
            meaningful_actions.join(
                event_memberships,
                meaningful_actions.c.user_id == event_memberships.c.user_id,
            )
        )
        .where(*event_filters)
    ).scalar_one()
    computed = int(total or 0)

    if not trust_on:
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
    """Return audience N for quorum. Quorum itself is required_votes(N).

    Per governance-rules.md:
    - platform-tagged → weekly unique active platform users
    - otherwise → weekly unique active users within project membership
    """
    if is_platform_tagged:
        return weekly_active_users_global(db)
    return weekly_active_project_members(db, project_id)


def resolve_event_vote_population(db: Session, event_id: UUID) -> int:
    """Return audience N for quorum. Quorum itself is required_votes(N).

    Per governance-rules.md:
    - platform-tagged → weekly unique active platform users
    - otherwise → weekly unique active users within event membership
    """
    if is_platform_event(db, event_id):
        return weekly_active_users_global(db)
    return weekly_active_event_members(db, event_id)


def project_uses_platform_vote_context(is_platform_tagged: bool) -> bool:
    return bool(is_platform_tagged)


def event_uses_platform_vote_context(db: Session, event_id: UUID) -> bool:
    return is_platform_event(db, event_id)


def can_cast_project_governance_vote(
    db: Session,
    *,
    project_id: UUID,
    user_id: UUID,
    is_platform_tagged: bool,
) -> bool:
    """Platform-tagged governance votes are open to any signed-in established user."""
    if not is_platform_tagged:
        membership = db.execute(
            select(project_memberships.c.user_id).where(
                project_memberships.c.project_id == project_id,
                project_memberships.c.user_id == user_id,
            )
        ).first()
        if membership is None:
            return False
    ensure_established_voter(db, user_id)
    return True


def can_cast_event_governance_vote(
    db: Session,
    *,
    event_id: UUID,
    user_id: UUID,
) -> bool:
    """Platform-tagged governance votes are open to any signed-in established user."""
    if not is_platform_event(db, event_id):
        membership = db.execute(
            select(event_memberships.c.user_id).where(
                event_memberships.c.event_id == event_id,
                event_memberships.c.user_id == user_id,
            )
        ).first()
        if membership is None:
            return False
    ensure_established_voter(db, user_id)
    return True
