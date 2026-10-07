"""Account-to-account trust ratios.

real_r is the earned vouch share and is display-only.
Stances, votes, quorum, and activity bands follow effective_r.
Bootstrap accounts use max(real gate ratio, fading floor) until the floor hits 0.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import account_stances, meaningful_actions, users

_CACHE_KEY = "trust_graph_v1"
_OPEN = "open"
_LIMITED = "limited"
_HEAVY = "heavy"
_INERT = "inert"

INERT_DETAIL = "This account is paused. Its trust ratio is under 0.10."
RATE_DETAIL = "This account is posting too quickly for its current trust ratio."


def vouch_detail(license_vouches: int) -> str:
    return (
        "Vouching needs a trust ratio of at least 0.66. "
        f"After the first accounts, it also needs {license_vouches} licensing vouches."
    )


def mark_detail(license_vouches: int) -> str:
    return (
        "Marking an account as a bot needs "
        f"{license_vouches} licensing vouches and a trust ratio of at least 0.66."
    )


SELF_STANCE_DETAIL = "You cannot take a stance on your own account."


@dataclass(frozen=True)
class TrustParams:
    vote_threshold: float = 0.66
    limited_threshold: float = 0.30
    inert_threshold: float = 0.10
    bootstrap_markers: int = 10
    bootstrap_target: int = 20
    license_vouches: int = 5
    min_bot_marks: int = 3

    @classmethod
    def from_settings(cls) -> TrustParams:
        settings = get_settings()
        return cls(
            vote_threshold=float(settings.governance_vote_threshold),
            limited_threshold=float(settings.governance_limited_threshold),
            inert_threshold=float(settings.governance_inert_threshold),
            bootstrap_markers=int(settings.governance_bootstrap_markers),
            bootstrap_target=int(settings.governance_bootstrap_target),
            license_vouches=int(settings.governance_mark_license_vouches),
            min_bot_marks=int(settings.governance_min_bot_marks),
        )


@dataclass(frozen=True)
class TrustAccount:
    user_id: UUID
    username: str
    completed_at: datetime | None
    layer2_active: bool
    profile_image_url: str | None = None


@dataclass(frozen=True)
class TrustStance:
    source_id: UUID
    target_id: UUID
    kind: str


@dataclass
class TrustAccountView:
    user_id: UUID
    username: str
    real_r: float
    effective_r: float
    vouch_weight: float
    bot_weight: float
    floor: float
    is_bootstrap: bool
    bootstrap_floor_active: bool
    licensed: bool
    counts: bool
    can_vote: bool
    can_vouch: bool
    can_mark_bot: bool
    participation: str
    layer2_active: bool
    voucher_usernames: tuple[str, ...]
    bot_marker_usernames: tuple[str, ...]
    voucher_image_urls: tuple[str | None, ...] = ()
    bot_marker_image_urls: tuple[str | None, ...] = ()


def trust_vote_detail() -> str:
    params = TrustParams.from_settings()
    return (
        "Voting needs a trust ratio of at least "
        f"{params.vote_threshold:.2f} and {params.license_vouches} vouches "
        "from accounts that already have that standing."
    )


def moderator_weight_detail() -> str:
    params = TrustParams.from_settings()
    return (
        f"Volunteering as a moderator needs vouch weight of at least {params.vote_threshold:.2f}."
    )


def compute_trust_graph(
    accounts: list[TrustAccount],
    stances: list[TrustStance],
    params: TrustParams,
) -> dict[UUID, TrustAccountView]:
    by_id = {account.user_id: account for account in accounts}
    names = {account.user_id: account.username for account in accounts}
    images = {account.user_id: account.profile_image_url for account in accounts}
    vouch_from: dict[UUID, list[UUID]] = defaultdict(list)
    bot_from: dict[UUID, list[UUID]] = defaultdict(list)
    vouch_to: dict[UUID, list[UUID]] = defaultdict(list)
    bot_to: dict[UUID, list[UUID]] = defaultdict(list)
    voucher_people: dict[UUID, list[tuple[str, str | None]]] = defaultdict(list)
    marker_people: dict[UUID, list[tuple[str, str | None]]] = defaultdict(list)
    incoming: set[UUID] = set()

    for stance in stances:
        if stance.source_id == stance.target_id:
            continue
        if stance.source_id not in by_id or stance.target_id not in by_id:
            continue
        if stance.kind == "vouch":
            vouch_from[stance.source_id].append(stance.target_id)
            vouch_to[stance.target_id].append(stance.source_id)
            voucher_people[stance.target_id].append(
                (names[stance.source_id], images.get(stance.source_id))
            )
            incoming.add(stance.target_id)
        elif stance.kind == "bot":
            bot_from[stance.source_id].append(stance.target_id)
            bot_to[stance.target_id].append(stance.source_id)
            marker_people[stance.target_id].append(
                (names[stance.source_id], images.get(stance.source_id))
            )
            incoming.add(stance.target_id)

    completed = sorted(
        (account for account in accounts if account.completed_at is not None),
        key=lambda account: (account.completed_at, str(account.user_id)),
    )
    marker_limit = max(0, params.bootstrap_markers)
    bootstrap_ids = {account.user_id for account in completed[:marker_limit]}

    locked_out: set[UUID] = set()
    views: dict[UUID, _RawView] = {}
    floor = 0.0
    for _ in range(8):
        views, floor, oscillating = _solve_weights(
            accounts,
            bootstrap_ids,
            vouch_from,
            bot_from,
            vouch_to,
            bot_to,
            incoming,
            params,
            locked_out,
        )
        if not oscillating:
            break
        locked_out |= oscillating

    licensed = _license_ids(views, vouch_to, bootstrap_ids, floor, params)
    result: dict[UUID, TrustAccountView] = {}
    for account in accounts:
        raw = views[account.user_id]
        is_licensed = account.user_id in licensed
        trusted = raw.effective_r + 1e-12 >= params.vote_threshold
        bootstrap_active = account.user_id in bootstrap_ids and floor > 1e-12
        can_vote = account.layer2_active and is_licensed and trusted
        # A floor under 0.66 must not freeze vouching. Bootstrap can still open
        # a vouch while the floor is above 0. Everyone else still needs 0.66.
        can_vouch = account.layer2_active and (bootstrap_active or (is_licensed and trusted))
        can_mark = account.layer2_active and is_licensed and trusted
        result[account.user_id] = TrustAccountView(
            user_id=account.user_id,
            username=account.username,
            real_r=raw.real_r,
            effective_r=raw.effective_r,
            vouch_weight=raw.vouch_weight,
            bot_weight=raw.bot_weight,
            floor=floor,
            is_bootstrap=account.user_id in bootstrap_ids,
            bootstrap_floor_active=(
                account.user_id in bootstrap_ids and raw.effective_r > raw.real_r + 1e-9
            ),
            licensed=is_licensed,
            counts=raw.counts,
            can_vote=can_vote,
            can_vouch=can_vouch,
            can_mark_bot=can_mark,
            participation=raw.participation,
            layer2_active=account.layer2_active,
            voucher_usernames=tuple(
                name for name, _image in sorted(voucher_people.get(account.user_id, []))
            ),
            bot_marker_usernames=tuple(
                name for name, _image in sorted(marker_people.get(account.user_id, []))
            ),
            voucher_image_urls=tuple(
                image for _name, image in sorted(voucher_people.get(account.user_id, []))
            ),
            bot_marker_image_urls=tuple(
                image for _name, image in sorted(marker_people.get(account.user_id, []))
            ),
        )
    return result


@dataclass
class _RawView:
    real_r: float
    effective_r: float
    vouch_weight: float
    bot_weight: float
    counts: bool
    participation: str
    is_bootstrap: bool


def _solve_weights(
    accounts: list[TrustAccount],
    bootstrap_ids: set[UUID],
    vouch_from: dict[UUID, list[UUID]],
    bot_from: dict[UUID, list[UUID]],
    vouch_to: dict[UUID, list[UUID]],
    bot_to: dict[UUID, list[UUID]],
    incoming: set[UUID],
    params: TrustParams,
    locked_out: set[UUID],
) -> tuple[dict[UUID, _RawView], float, set[UUID] | None]:
    budgets = {account.user_id: 1.0 for account in accounts if account.user_id not in locked_out}
    seen: dict[tuple, int] = {}
    history: list[set[UUID]] = []
    views: dict[UUID, _RawView] = {}
    floor = _bootstrap_floor(0, params.bootstrap_target) if bootstrap_ids else 0.0

    for _ in range(32):
        vouch_w, bot_w, units = _deliver(budgets, vouch_from, bot_from, vouch_to, bot_to)
        views, new_budgets, floor_next = _derive(
            accounts,
            bootstrap_ids,
            vouch_w,
            bot_w,
            units,
            incoming,
            params,
            locked_out,
        )
        if _budgets_match(budgets, new_budgets) and abs(floor_next - floor) < 1e-6:
            return views, floor_next, None
        key = _budget_key(floor_next, new_budgets)
        if key in seen:
            start = seen[key]
            common: set[UUID] | None = None
            union: set[UUID] = set()
            for ids in history[start:]:
                union |= ids
                common = set(ids) if common is None else common & ids
            oscillating = union - (common or set())
            if not oscillating:
                oscillating = set(union)
            return views, floor_next, oscillating
        seen[key] = len(history)
        history.append(set(new_budgets))
        budgets = new_budgets
        floor = floor_next
    return views, floor, None


def _deliver(
    budgets: dict[UUID, float],
    vouch_from: dict[UUID, list[UUID]],
    bot_from: dict[UUID, list[UUID]],
    vouch_to: dict[UUID, list[UUID]],
    bot_to: dict[UUID, list[UUID]],
) -> tuple[dict[UUID, float], dict[UUID, float], dict[UUID, int]]:
    vouch_w: dict[UUID, float] = defaultdict(float)
    for source, budget in budgets.items():
        if budget <= 0:
            continue
        targets = vouch_from.get(source, [])
        if not targets:
            continue
        share = budget / len(targets)
        for target in targets:
            vouch_w[target] += share

    bot_w: dict[UUID, float] = defaultdict(float)
    units: dict[UUID, int] = defaultdict(int)
    for target, markers in bot_to.items():
        active = [marker for marker in markers if budgets.get(marker, 0.0) > 0]
        if not active:
            continue
        marker_set = set(active)
        independent: list[UUID] = []
        pooled: list[UUID] = []
        for marker in active:
            sources = [
                source for source in vouch_to.get(marker, []) if budgets.get(source, 0.0) > 0
            ]
            outside = any(source not in marker_set for source in sources)
            if not sources or outside:
                independent.append(marker)
            else:
                pooled.append(marker)
        delivered = 0.0
        for marker in independent:
            targets = bot_from.get(marker, [])
            if targets:
                delivered += budgets[marker] / len(targets)
        if pooled:
            pooled_sum = 0.0
            for marker in pooled:
                targets = bot_from.get(marker, [])
                if targets:
                    pooled_sum += budgets[marker] / len(targets)
            delivered += min(1.0, pooled_sum)
        bot_w[target] = delivered
        units[target] = len(independent) + (1 if pooled else 0)
    return vouch_w, bot_w, units


def _derive(
    accounts: list[TrustAccount],
    bootstrap_ids: set[UUID],
    vouch_w: dict[UUID, float],
    bot_w: dict[UUID, float],
    units: dict[UUID, int],
    incoming: set[UUID],
    params: TrustParams,
    locked_out: set[UUID],
) -> tuple[dict[UUID, _RawView], dict[UUID, float], float]:
    mature = 0
    preliminary: list[tuple[TrustAccount, float, float, float, float, float]] = []
    for account in accounts:
        vouch_weight = vouch_w.get(account.user_id, 0.0)
        bot_weight = bot_w.get(account.user_id, 0.0)
        real_r = _ratio(vouch_weight, bot_weight)
        gate_bot = (
            bot_weight if units.get(account.user_id, 0) >= max(1, params.min_bot_marks) else 0.0
        )
        if params.min_bot_marks <= 0:
            gate_bot = bot_weight
        gate_r = _ratio(vouch_weight, gate_bot)
        if account.user_id not in bootstrap_ids and real_r + 1e-12 >= params.vote_threshold:
            mature += 1
        preliminary.append((account, real_r, gate_r, vouch_weight, bot_weight, gate_bot))

    floor = _bootstrap_floor(mature, params.bootstrap_target) if bootstrap_ids else 0.0
    views: dict[UUID, _RawView] = {}
    budgets: dict[UUID, float] = {}
    for account, real_r, gate_r, vouch_weight, bot_weight, gate_bot in preliminary:
        is_bootstrap = account.user_id in bootstrap_ids
        if is_bootstrap and floor > 1e-12:
            effective_r = max(gate_r, floor)
        else:
            effective_r = gate_r
        if account.user_id in locked_out:
            counts = False
        elif is_bootstrap and floor > 1e-12:
            counts = True
        else:
            counts = effective_r + 1e-12 >= params.vote_threshold
        if counts:
            budgets[account.user_id] = effective_r
        views[account.user_id] = _RawView(
            real_r=real_r,
            effective_r=effective_r,
            vouch_weight=vouch_weight,
            bot_weight=bot_weight,
            counts=counts,
            participation=_participation(
                account.user_id in incoming,
                effective_r,
                vouch_weight,
                gate_bot,
                params,
            ),
            is_bootstrap=is_bootstrap,
        )
    return views, budgets, floor


def _license_ids(
    views: dict[UUID, _RawView],
    vouch_to: dict[UUID, list[UUID]],
    bootstrap_ids: set[UUID],
    floor: float,
    params: TrustParams,
) -> set[UUID]:
    licensed: set[UUID] = set()
    if params.license_vouches <= 0:
        return {
            user_id
            for user_id, view in views.items()
            if view.effective_r + 1e-12 >= params.vote_threshold
        }
    changed = True
    while changed:
        changed = False
        for user_id, view in views.items():
            if user_id in licensed or view.effective_r + 1e-12 < params.vote_threshold:
                continue
            qualified = 0
            for source in vouch_to.get(user_id, []):
                source_view = views.get(source)
                if source_view is None or not source_view.counts:
                    continue
                bootstrap_active = source in bootstrap_ids and floor > 1e-12
                if bootstrap_active or source in licensed:
                    qualified += 1
            if qualified >= params.license_vouches:
                licensed.add(user_id)
                changed = True
    return licensed


def _participation(
    has_incoming: bool,
    effective_r: float,
    vouch_weight: float,
    gate_bot: float,
    params: TrustParams,
) -> str:
    if not has_incoming or (gate_bot <= 0 and vouch_weight <= 0):
        return _OPEN
    if effective_r < params.inert_threshold:
        return _INERT
    if effective_r < params.limited_threshold:
        return _HEAVY
    if effective_r < params.vote_threshold:
        return _LIMITED
    return _OPEN


def _ratio(vouch_weight: float, bot_weight: float) -> float:
    total = vouch_weight + bot_weight
    if total <= 0:
        return 0.0
    return vouch_weight / total


def _bootstrap_floor(mature: int, target: int) -> float:
    if target <= 0 or mature >= target:
        return 0.0
    return max(0.0, 1.0 - (mature / target))


def _budgets_match(left: dict[UUID, float], right: dict[UUID, float]) -> bool:
    if set(left) != set(right):
        return False
    return all(abs(left[key] - right[key]) < 1e-6 for key in left)


def _budget_key(floor: float, budgets: dict[UUID, float]) -> tuple:
    items = tuple(sorted((str(user_id), round(budget, 4)) for user_id, budget in budgets.items()))
    return (round(floor, 4), items)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _load_accounts(db: Session) -> list[TrustAccount]:
    from app.utils.votes import (
        CONTENT_REMOVED_ACTION,
        governance_guardrails_enabled,
        meaningful_action_types,
    )

    settings = get_settings()
    now = datetime.now(UTC)
    rows = db.execute(
        select(users.c.id, users.c.username, users.c.created_at, users.c.profile_image_url)
    ).all()
    guardrails = governance_guardrails_enabled()
    min_actions = settings.governance_min_meaningful_actions if guardrails else 0
    min_age = timedelta(
        hours=max(settings.governance_min_account_age_hours, 0) if guardrails else 0
    )
    penalty_hours = settings.governance_removal_penalty_hours if guardrails else 0
    counted = meaningful_action_types() if guardrails else frozenset()
    action_times: dict[UUID, list[datetime]] = defaultdict(list)
    penalties: dict[UUID, datetime] = {}
    wanted = set(counted)
    if penalty_hours > 0:
        wanted.add(CONTENT_REMOVED_ACTION)
    if guardrails and wanted:
        action_rows = db.execute(
            select(
                meaningful_actions.c.user_id,
                meaningful_actions.c.action_type,
                meaningful_actions.c.occurred_at,
            ).where(meaningful_actions.c.action_type.in_(wanted))
        ).all()
        for user_id, action_type, occurred_at in action_rows:
            when = _aware(occurred_at)
            if action_type == CONTENT_REMOVED_ACTION:
                previous = penalties.get(user_id)
                if previous is None or when > previous:
                    penalties[user_id] = when
            if action_type in counted:
                action_times[user_id].append(when)

    accounts: list[TrustAccount] = []
    for user_id, username, created_at, profile_image_url in rows:
        created = _aware(created_at)
        times = sorted(action_times.get(user_id, []))
        if not guardrails:
            accounts.append(TrustAccount(user_id, username, created, True, profile_image_url))
            continue
        if min_actions <= 0:
            action_ready: datetime | None = created
        elif len(times) >= min_actions:
            action_ready = times[min_actions - 1]
        else:
            action_ready = None
        if action_ready is None:
            completed = None
        else:
            completed = max(created + min_age, action_ready)
            if completed > now:
                completed = None
        effective_start = created
        latest_penalty = penalties.get(user_id)
        if penalty_hours > 0 and latest_penalty is not None:
            penalty_start = latest_penalty + timedelta(hours=penalty_hours)
            if penalty_start > effective_start:
                effective_start = penalty_start
        active = now - effective_start >= min_age and (
            min_actions <= 0 or len(times) >= min_actions
        )
        if completed is None:
            active = False
        accounts.append(TrustAccount(user_id, username, completed, active, profile_image_url))
    return accounts


def _load_stances(db: Session) -> list[TrustStance]:
    rows = db.execute(
        select(
            account_stances.c.source_user_id,
            account_stances.c.target_user_id,
            account_stances.c.stance,
        )
    ).all()
    return [TrustStance(source, target, stance) for source, target, stance in rows]


def load_trust_graph(db: Session) -> dict[UUID, TrustAccountView]:
    cached = db.info.get(_CACHE_KEY)
    if cached is not None:
        return cached
    graph = compute_trust_graph(_load_accounts(db), _load_stances(db), TrustParams.from_settings())
    db.info[_CACHE_KEY] = graph
    return graph


def invalidate_trust_graph(db: Session) -> None:
    db.info.pop(_CACHE_KEY, None)


def user_can_vote(db: Session, user_id: UUID) -> bool:
    view = load_trust_graph(db).get(user_id)
    return bool(view and view.can_vote)


def voting_user_ids(db: Session) -> list[UUID]:
    return [user_id for user_id, view in load_trust_graph(db).items() if view.can_vote]


def user_vouch_weight(db: Session, user_id: UUID) -> float:
    view = load_trust_graph(db).get(user_id)
    if view is None:
        return 0.0
    return view.vouch_weight


def ensure_can_participate(db: Session, user_id: UUID) -> None:
    if not get_settings().governance_trust_ratio_enabled:
        return
    view = load_trust_graph(db).get(user_id)
    if view is None or view.participation == _OPEN:
        return
    if view.participation == _INERT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INERT_DETAIL)
    limit = _participation_limit(view)
    try:
        from app.cache import get_sync_redis_client

        redis = get_sync_redis_client()
        key = f"trust:participate:{user_id}"
        current = int(redis.incr(key))
        if current == 1:
            redis.expire(key, 60)
        if current > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=RATE_DETAIL)
    except HTTPException:
        raise
    except Exception:
        return


def _participation_limit(view: TrustAccountView) -> int:
    if view.participation == _HEAVY:
        return max(1, int(view.effective_r * 10))
    return max(2, int(view.effective_r * 30))


def _round_ratio(value: float) -> float:
    return round(float(value), 4)


def trust_summaries(db: Session) -> dict[UUID, dict[str, object]]:
    graph = load_trust_graph(db)
    return {
        user_id: {
            "real_r": _round_ratio(view.real_r),
            "bootstrap_floor": view.bootstrap_floor_active,
        }
        for user_id, view in graph.items()
    }


def _trust_people(
    usernames: tuple[str, ...], images: tuple[str | None, ...]
) -> list[dict[str, object]]:
    people: list[dict[str, object]] = []
    for index, username in enumerate(usernames):
        image = images[index] if index < len(images) else None
        people.append({"username": username, "profile_image_url": image})
    return people


def account_trust_payload(
    db: Session,
    target_id: UUID,
    viewer_id: UUID | None,
) -> dict[str, object]:
    graph = load_trust_graph(db)
    view = graph.get(target_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    viewer = graph.get(viewer_id) if viewer_id is not None else None
    stance = None
    if viewer_id is not None and viewer_id != target_id:
        stance = db.execute(
            select(account_stances.c.stance).where(
                account_stances.c.source_user_id == viewer_id,
                account_stances.c.target_user_id == target_id,
            )
        ).scalar_one_or_none()
    same = viewer_id is not None and viewer_id == target_id
    return {
        "real_r": _round_ratio(view.real_r),
        "vouch_weight": _round_ratio(view.vouch_weight),
        "bot_weight": _round_ratio(view.bot_weight),
        "bootstrap_floor": view.bootstrap_floor_active,
        "bootstrap_floor_value": (
            _round_ratio(view.floor) if view.bootstrap_floor_active else None
        ),
        "vouchers": _trust_people(view.voucher_usernames, view.voucher_image_urls),
        "bot_markers": _trust_people(view.bot_marker_usernames, view.bot_marker_image_urls),
        "viewer_stance": stance,
        "viewer_can_vouch": bool(viewer and not same and viewer.can_vouch),
        "viewer_can_mark_bot": bool(viewer and not same and viewer.can_mark_bot),
        "viewer_can_clear": stance is not None,
    }


def apply_account_stance(
    db: Session,
    actor_id: UUID,
    target_id: UUID,
    stance: str,
) -> dict[str, object]:
    if actor_id == target_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SELF_STANCE_DETAIL)
    graph = load_trust_graph(db)
    actor = graph.get(actor_id)
    if actor is None or target_id not in graph:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if stance == "clear":
        db.execute(
            delete(account_stances).where(
                account_stances.c.source_user_id == actor_id,
                account_stances.c.target_user_id == target_id,
            )
        )
    elif stance == "vouch":
        if not actor.can_vouch:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=vouch_detail(get_settings().governance_mark_license_vouches),
            )
        _upsert_stance(db, actor_id, target_id, "vouch")
    elif stance == "bot":
        if not actor.can_mark_bot:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=mark_detail(get_settings().governance_mark_license_vouches),
            )
        _upsert_stance(db, actor_id, target_id, "bot")
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown stance"
        )
    db.commit()
    invalidate_trust_graph(db)
    return account_trust_payload(db, target_id, actor_id)


def _upsert_stance(db: Session, actor_id: UUID, target_id: UUID, stance: str) -> None:
    now = datetime.now(UTC)
    db.execute(
        pg_insert(account_stances)
        .values(
            source_user_id=actor_id,
            target_user_id=target_id,
            stance=stance,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[
                account_stances.c.source_user_id,
                account_stances.c.target_user_id,
            ],
            set_={"stance": stance, "updated_at": now},
        )
    )
