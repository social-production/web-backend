"""Voting standing: a light Sybil guard that never locks newcomers out of taking part.

new     – default. Join, post, chat, take roles, signal, rate.
warm    – passed the warm-up (account age + a few real actions). Votes count at a reduced
          weight and are left out of the quorum population, so a swarm of fresh accounts can
          neither fake nor block quorum.
full    – warm, and earned standing once: a vouch from a full member, or attending an
          activity that another participant rated. Votes count fully and count for quorum.
          The first accounts on the platform start full so there is someone to vouch.
paused  – bot marks from full members outnumber vouches (with at least a minimum of marks).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    account_stances,
    event_activities,
    event_activity_assignments,
    event_activity_ratings,
    event_activity_roles,
    project_activities,
    project_activity_assignments,
    project_activity_ratings,
    project_activity_roles,
    users,
)

NEW = "new"
WARM = "warm"
FULL = "full"
PAUSED = "paused"


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


def _attended_ids(db: Session) -> set:
    """Users assigned to an ended activity that another participant rated."""
    now = datetime.now(UTC)
    attended: set = set()
    for activities, roles, assignments, ratings in (
        (
            project_activities,
            project_activity_roles,
            project_activity_assignments,
            project_activity_ratings,
        ),
        (
            event_activities,
            event_activity_roles,
            event_activity_assignments,
            event_activity_ratings,
        ),
    ):
        rows = db.execute(
            select(assignments.c.user_id)
            .select_from(
                assignments.join(roles, roles.c.id == assignments.c.role_id).join(
                    activities, activities.c.id == roles.c.activity_id
                )
            )
            .join(
                ratings,
                and_(
                    ratings.c.activity_id == activities.c.id,
                    ratings.c.user_id != assignments.c.user_id,
                ),
            )
            .where(activities.c.ends_at < now)
            .distinct()
        ).scalars()
        attended.update(rows)
    return attended


def compute_standings(db: Session) -> dict:
    """Standing for every account. Cheap enough per request at this platform's size."""
    from app.utils.votes import governance_guardrails_enabled

    settings = get_settings()
    all_ids = set(db.execute(select(users.c.id)).scalars())
    if not governance_guardrails_enabled():
        # Warm-up switched off: everyone votes at full weight, as before standing existed.
        return {user_id: Standing(FULL, 1.0) for user_id in all_ids}
    warm_weight = float(settings.standing_warm_weight)
    min_marks = max(1, int(settings.governance_min_bot_marks))

    warm = _warm_ids(db)
    bootstrap = _bootstrap_ids(db)
    stances = db.execute(
        select(
            account_stances.c.source_user_id,
            account_stances.c.target_user_id,
            account_stances.c.stance,
        )
    ).all()
    vouchers: dict = {}
    for source, target, stance in stances:
        if stance == "vouch":
            vouchers.setdefault(target, set()).add(source)

    full = set(bootstrap) | (_attended_ids(db) & warm)
    changed = True
    while changed:
        changed = False
        for user_id in warm - full:
            if vouchers.get(user_id, set()) & full:
                full.add(user_id)
                changed = True

    marks: dict = {}
    vouch_counts: dict = {}
    for source, target, stance in stances:
        if source not in full:
            continue
        if stance == "bot":
            marks[target] = marks.get(target, 0) + 1
        elif stance == "vouch":
            vouch_counts[target] = vouch_counts.get(target, 0) + 1

    result: dict = {}
    for user_id in all_ids:
        bot_marks = marks.get(user_id, 0)
        if (
            user_id not in bootstrap
            and bot_marks >= min_marks
            and bot_marks > vouch_counts.get(user_id, 0)
        ):
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
        return "Your votes count half for now. One vouch, or taking part in an activity someone else rates, makes them count fully."
    if level == PAUSED:
        return "Members marked this account as a bot, so its votes are paused."
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
