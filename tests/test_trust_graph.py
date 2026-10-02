"""Layer 3 trust ratios: split weight, bootstrap floor, license of 5."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.trust import (
    TrustAccount,
    TrustParams,
    TrustStance,
    compute_trust_graph,
)
from app.services.users import set_account_stance
from app.utils.votes import is_established_voter
from tests.conftest import seed_user

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def _params(**overrides: object) -> TrustParams:
    base = dict(
        vote_threshold=0.66,
        limited_threshold=0.30,
        inert_threshold=0.10,
        bootstrap_markers=10,
        bootstrap_target=20,
        license_vouches=5,
        min_bot_marks=3,
    )
    base.update(overrides)
    return TrustParams(**base)


def _account(index: int, *, active: bool = True) -> TrustAccount:
    return TrustAccount(
        user_id=uuid4(),
        username=f"user-{index}",
        completed_at=NOW + timedelta(seconds=index),
        layer2_active=active,
    )


def _vouch(source: TrustAccount, target: TrustAccount) -> TrustStance:
    return TrustStance(source.user_id, target.user_id, "vouch")


def _bot(source: TrustAccount, target: TrustAccount) -> TrustStance:
    return TrustStance(source.user_id, target.user_id, "bot")


def _clique(accounts: list[TrustAccount]) -> list[TrustStance]:
    stances: list[TrustStance] = []
    for source in accounts:
        for target in accounts:
            if source.user_id != target.user_id:
                stances.append(_vouch(source, target))
    return stances


def test_genesis_bootstrap_budgets_are_not_zero() -> None:
    accounts = [_account(index) for index in range(10)]
    graph = compute_trust_graph(accounts, [], _params())

    for account in accounts:
        view = graph[account.user_id]
        assert view.real_r == pytest.approx(0)
        assert view.effective_r == pytest.approx(1)
        assert view.floor == pytest.approx(1)
        assert view.is_bootstrap
        assert view.bootstrap_floor_active
        assert view.counts
        assert view.can_vouch
        assert not view.can_mark_bot
        assert not view.licensed
        assert not view.can_vote
        assert view.participation == "open"


def test_bootstrap_can_license_each_other() -> None:
    accounts = [_account(index) for index in range(10)]
    graph = compute_trust_graph(accounts, _clique(accounts), _params())

    for account in accounts:
        view = graph[account.user_id]
        assert view.vouch_weight == pytest.approx(1)
        assert view.real_r == pytest.approx(1)
        assert view.effective_r == pytest.approx(1)
        assert view.licensed
        assert view.can_vote
        assert view.can_mark_bot
        assert not view.bootstrap_floor_active


def test_one_budget_is_split_across_targets() -> None:
    supporters = [_account(index) for index in range(6)]
    source = _account(6)
    targets = [_account(index) for index in range(7, 107)]
    stances = _clique(supporters)
    stances.extend(_vouch(supporter, source) for supporter in supporters)
    stances.extend(_vouch(source, target) for target in targets)
    graph = compute_trust_graph(
        [*supporters, source, *targets],
        stances,
        _params(bootstrap_markers=0),
    )

    for target in targets:
        view = graph[target.user_id]
        assert view.vouch_weight == pytest.approx(0.01)
        assert view.real_r == pytest.approx(1)
        assert not view.licensed
        assert not view.can_vote


def test_license_needs_five_counting_vouchers() -> None:
    bootstrap = [_account(index) for index in range(10)]
    ordinary = _account(20)
    stances = _clique(bootstrap)
    for source in bootstrap[:4]:
        stances.append(_vouch(source, ordinary))
    short = compute_trust_graph([*bootstrap, ordinary], stances, _params())
    assert not short[ordinary.user_id].licensed
    assert not short[ordinary.user_id].can_vote

    stances.append(_vouch(bootstrap[4], ordinary))
    full = compute_trust_graph([*bootstrap, ordinary], stances, _params())
    assert full[ordinary.user_id].licensed
    assert full[ordinary.user_id].can_vote
    assert full[ordinary.user_id].real_r == pytest.approx(1)


def test_closed_clique_keeps_weight_without_a_vote() -> None:
    clique = [_account(index) for index in range(6)]
    graph = compute_trust_graph(clique, _clique(clique), _params(bootstrap_markers=0))

    for account in clique:
        view = graph[account.user_id]
        assert view.vouch_weight == pytest.approx(1)
        assert view.real_r == pytest.approx(1)
        assert not view.licensed
        assert not view.can_vote


def test_accuser_pile_collapses_to_one_mark() -> None:
    pile = [_account(index) for index in range(6)]
    victim = _account(30)
    stances = _clique(pile)
    stances.extend(_bot(marker, victim) for marker in pile)
    graph = compute_trust_graph(
        [*pile, victim],
        stances,
        _params(bootstrap_markers=0),
    )

    view = graph[victim.user_id]
    assert view.bot_weight == pytest.approx(1)
    assert view.real_r == pytest.approx(0)
    assert view.participation == "open"


def test_three_independent_marks_can_pause_an_account() -> None:
    founders = [_account(0), _account(1)]
    pile = [_account(index) for index in range(2, 8)]
    victim = _account(30)
    stances = _clique(pile)
    stances.extend(_bot(marker, victim) for marker in pile)
    stances.append(_bot(founders[0], victim))
    stances.append(_bot(founders[1], victim))
    graph = compute_trust_graph(
        [*founders, *pile, victim],
        stances,
        _params(bootstrap_markers=2),
    )

    view = graph[victim.user_id]
    assert view.participation == "inert"
    assert view.effective_r < 0.10


def test_bootstrap_stances_still_count_at_half_floor() -> None:
    founder = _account(0)
    mature = [_account(index) for index in range(1, 11)]
    stances = _clique(mature)
    stances.append(_vouch(founder, mature[0]))
    graph = compute_trust_graph([founder, *mature], stances, _params(bootstrap_markers=1))

    founder_view = graph[founder.user_id]
    assert founder_view.floor == pytest.approx(0.5)
    assert founder_view.real_r == pytest.approx(0)
    assert founder_view.effective_r == pytest.approx(0.5)
    assert founder_view.counts
    assert founder_view.can_vouch
    assert not founder_view.can_vote
    assert graph[mature[0].user_id].vouch_weight == pytest.approx(1.5)


def test_bootstrap_floor_reaches_zero_at_the_target() -> None:
    founder = _account(0)
    mature = [_account(index) for index in range(1, 21)]
    graph = compute_trust_graph(
        [founder, *mature],
        _clique(mature),
        _params(bootstrap_markers=1),
    )

    founder_view = graph[founder.user_id]
    assert founder_view.floor == pytest.approx(0)
    assert founder_view.effective_r == pytest.approx(0)
    assert not founder_view.counts
    assert not founder_view.bootstrap_floor_active


def test_activity_band_follows_effective_ratio_not_real_ratio() -> None:
    founder = _account(0)
    markers = [_account(index) for index in range(1, 5)]
    stances = _clique(markers)
    stances.extend(_bot(marker, founder) for marker in markers)
    graph = compute_trust_graph(
        [founder, *markers],
        stances,
        _params(bootstrap_markers=1, min_bot_marks=1),
    )

    view = graph[founder.user_id]
    assert view.real_r < 0.10
    assert view.floor == pytest.approx(0.8)
    assert view.effective_r == pytest.approx(0.8)
    assert view.participation == "open"
    assert view.bootstrap_floor_active
    assert view.counts


def test_bootstrap_floor_keeps_a_marked_founder_trusted() -> None:
    founder = _account(0)
    clique = [_account(index) for index in range(1, 5)]
    voucher = _account(5)
    marker = _account(6)
    stances = _clique(clique)
    for member in clique:
        stances.append(_vouch(member, voucher))
        stances.append(_vouch(member, marker))
    stances.append(_vouch(voucher, founder))
    stances.append(_bot(marker, founder))
    graph = compute_trust_graph(
        [founder, *clique, voucher, marker],
        stances,
        _params(bootstrap_markers=1, min_bot_marks=1),
    )

    view = graph[founder.user_id]
    assert view.real_r == pytest.approx(0.5)
    assert view.effective_r == pytest.approx(max(view.real_r, view.floor))
    assert view.floor > view.real_r
    assert view.participation == "open"
    assert view.counts


def test_ratio_switch_off_leaves_voting_open(db_transaction) -> None:
    user_id, _username = seed_user(db_transaction, username_prefix="trust-off")
    assert get_settings().governance_trust_ratio_enabled is False
    assert is_established_voter(db_transaction, user_id)


def test_ratio_switch_on_blocks_an_unlicensed_account(db_transaction, monkeypatch) -> None:
    user_id, _username = seed_user(db_transaction, username_prefix="trust-on")
    monkeypatch.setattr(get_settings(), "governance_trust_ratio_enabled", True)
    assert is_established_voter(db_transaction, user_id) is False


def test_stance_write_requires_a_counting_voucher(db_transaction, monkeypatch) -> None:
    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    settings = get_settings()
    monkeypatch.setattr(settings, "governance_bootstrap_markers", 0)
    actor_id, _actor = seed_user(db_transaction, username_prefix="trust-actor")
    _target_id, target_name = seed_user(db_transaction, username_prefix="trust-target")

    with pytest.raises(HTTPException) as exc:
        set_account_stance(db_transaction, actor_id, target_name, "vouch")
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as mark_exc:
        set_account_stance(db_transaction, actor_id, target_name, "bot")
    assert mark_exc.value.status_code == 403


def test_bootstrap_can_vouch_before_the_license_of_five(db_transaction, monkeypatch) -> None:
    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    settings = get_settings()
    monkeypatch.setattr(settings, "governance_bootstrap_markers", 100_000)
    monkeypatch.setattr(settings, "governance_bootstrap_target", 100_000)
    actor_id, _actor = seed_user(db_transaction, username_prefix="trust-boot")
    _target_id, target_name = seed_user(db_transaction, username_prefix="trust-peer")

    payload = set_account_stance(db_transaction, actor_id, target_name, "vouch")
    assert payload["viewer_stance"] == "vouch"
    assert payload["viewer_can_clear"] is True

    with pytest.raises(HTTPException) as exc:
        set_account_stance(db_transaction, actor_id, target_name, "bot")
    assert exc.value.status_code == 403
