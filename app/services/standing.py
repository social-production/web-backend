"""Voting standing: a light Sybil guard that never locks newcomers out of taking part.

new     – default. Join, post, chat, take roles, signal, rate.
warm    – past the warm-up (1 day + 3 contributions). Votes count half and are left out of the
          quorum population, so a swarm of fresh accounts can neither fake nor block quorum.
full    – warm, plus one of: a vouch from a full member; a service request the other person
          marked complete; or steady activity (account 30+ days old, active on 10 different
          days). The first accounts on the platform start full so there is someone to vouch.
paused  – a public bot review (app/services/bot_reviews.py) found the account is a bot.
          Read-only; its votes count for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    account_stances,
    bot_reviews,
    meaningful_actions,
    project_activity_assignments,
    project_activity_roles,
    project_service_history_completions,
    project_service_requests,
    users,
)

NEW = "new"
WARM = "warm"
FULL = "full"
PAUSED = "paused"

STEADY_MIN_AGE_DAYS = 30
STEADY_ACTIVE_DAYS = 10


@dataclass(frozen=True)
class Standing:
    level: str
    weight: float

    @property
    def can_vote(self) -> bool:
        return self.level in (WARM, FULL)

    @property
    def counts_for_quorum(self) -> bool:
        return self.level == FULL


def _warm_ids(db: Session) -> set:
    """Accounts past the warm-up. Reuses the governance warm-up rules in utils/votes.py."""
    from app.utils.votes import _established_user_id_select

    established = _established_user_id_select()
    if established is None:
        return set(db.execute(select(users.c.id)).scalars())
    return set(db.execute(established).scalars())


def _bootstrap_ids(db: Session) -> set:
    limit = max(0, int(get_settings().governance_bootstrap_markers))
    if limit == 0:
        return set()
    return set(
        db.execute(
            select(users.c.id).order_by(users.c.created_at, users.c.id).limit(limit)
        ).scalars()
    )


def paused_ids(db: Session) -> set:
    """Accounts whose most recent finished bot review said 'bot'."""
    latest = (
        select(
            bot_reviews.c.target_user_id,
            bot_reviews.c.status,
            func.row_number()
            .over(
                partition_by=bot_reviews.c.target_user_id,
                order_by=bot_reviews.c.closed_at.desc(),
            )
            .label("rank"),
        )
        .where(bot_reviews.c.status.in_(("upheld", "cleared")))
        .subquery()
    )
    return set(
        db.execute(
            select(latest.c.target_user_id).where(latest.c.rank == 1, latest.c.status == "upheld")
        ).scalars()
    )


def _service_completed_ids(db: Session) -> set:
    """The other side of a service request that was marked complete.

    A completion row records who marked it: the requester (then the activity's assigned
    providers earn standing) or a provider (then the requester does). The key is the activity id.
    """
    rows = db.execute(
        select(
            project_service_history_completions.c.role,
            project_service_history_completions.c.history_item_key,
        ).where(project_service_history_completions.c.completion_state == "completed")
    ).all()
    by_requester: set = set()
    by_provider: set = set()
    for role, key in rows:
        try:
            activity_id = UUID(str(key))
        except ValueError:
            continue
        (by_requester if role == "requester" else by_provider).add(activity_id)
    earned: set = set()
    if by_requester:
        earned.update(
            db.execute(
                select(project_activity_assignments.c.user_id)
                .join(
                    project_activity_roles,
                    project_activity_roles.c.id == project_activity_assignments.c.role_id,
                )
                .where(project_activity_roles.c.activity_id.in_(by_requester))
            ).scalars()
        )
    if by_provider:
        earned.update(
            db.execute(
                select(project_service_requests.c.requester_id).where(
                    project_service_requests.c.linked_activity_id.in_(by_provider)
                )
            ).scalars()
        )
    return earned


def _steady_ids(db: Session) -> set:
    from app.utils.votes import meaningful_action_types

    cutoff = datetime.now(UTC) - timedelta(days=STEADY_MIN_AGE_DAYS)
    active_days = func.count(func.distinct(func.date(meaningful_actions.c.occurred_at)))
    return set(
        db.execute(
            select(meaningful_actions.c.user_id)
            .join(users, users.c.id == meaningful_actions.c.user_id)
            .where(
                users.c.created_at <= cutoff,
                meaningful_actions.c.action_type.in_(meaningful_action_types()),
            )
            .group_by(meaningful_actions.c.user_id)
            .having(active_days >= STEADY_ACTIVE_DAYS)
        ).scalars()
    )


def compute_standings(db: Session) -> dict:
    """Standing for every account. Cheap enough per request at this platform's size."""
    from app.utils.votes import governance_guardrails_enabled

    settings = get_settings()
    all_ids = set(db.execute(select(users.c.id)).scalars())
    paused = paused_ids(db)
    if not governance_guardrails_enabled():
        # Warm-up switched off: everyone votes at full weight, as before standing existed.
        return {
            user_id: Standing(PAUSED, 0.0) if user_id in paused else Standing(FULL, 1.0)
            for user_id in all_ids
        }

    warm = _warm_ids(db)
    vouchers: dict = {}
    for source, target in db.execute(
        select(account_stances.c.source_user_id, account_stances.c.target_user_id).where(
            account_stances.c.stance == "vouch"
        )
    ).all():
        vouchers.setdefault(target, set()).add(source)

    full = (_bootstrap_ids(db) | (warm & (_service_completed_ids(db) | _steady_ids(db)))) - paused
    changed = True
    while changed:
        changed = False
        for user_id in warm - full - paused:
            if vouchers.get(user_id, set()) & full:
                full.add(user_id)
                changed = True

    warm_weight = float(settings.standing_warm_weight)
    result: dict = {}
    for user_id in all_ids:
        if user_id in paused:
            result[user_id] = Standing(PAUSED, 0.0)
        elif user_id in full:
            result[user_id] = Standing(FULL, 1.0)
        elif user_id in warm:
            result[user_id] = Standing(WARM, warm_weight)
        else:
            result[user_id] = Standing(NEW, 0.0)
    return result


def standing_for(db: Session, user_id) -> Standing:
    return compute_standings(db).get(user_id, Standing(NEW, 0.0))


def weights_for(db: Session, user_ids) -> dict:
    standings = compute_standings(db)
    return {user_id: standings.get(user_id, Standing(NEW, 0.0)).weight for user_id in user_ids}


def full_member_ids(db: Session) -> list:
    return [
        user_id for user_id, standing in compute_standings(db).items() if standing.counts_for_quorum
    ]


def next_step(db: Session, user_id) -> str:
    level = standing_for(db, user_id).level
    settings = get_settings()
    if level == FULL:
        return "Your votes count fully and you can vouch for others."
    if level == WARM:
        return (
            "Your votes count half for now. They count fully after one vouch from a full "
            "member, a completed service request, or being active on 10 different days."
        )
    if level == PAUSED:
        return "A community review found this account is a bot, so it is read-only."
    hours = max(0, int(settings.governance_min_account_age_hours))
    wait = (
        f"{hours // 24} day{'s' if hours // 24 != 1 else ''}"
        if hours >= 24
        else f"{hours} hour{'s' if hours != 1 else ''}"
    )
    actions = max(0, int(settings.governance_min_meaningful_actions))
    return (
        f"Voting opens after {wait} and {actions} contribution{'s' if actions != 1 else ''}, "
        "like commenting, posting or starting a project."
    )


def ensure_not_paused(db: Session, user_id) -> None:
    from fastapi import HTTPException, status

    if user_id in paused_ids(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A community review found this account is a bot, so it is read-only.",
        )
