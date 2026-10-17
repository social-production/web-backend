"""Standing: warm-up, half-weight warm votes, earned full standing, bot pause."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    account_stances,
    meaningful_actions,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_phase_change_votes,
    project_service_history_completions,
    projects,
    users,
)
from app.services.bot_reviews import (
    cast_vote,
    marker_on_cooldown,
    maybe_open_review,
    review_payload,
    vouch_banned_until,
)
from app.services.governance_votes import compute_vote_summary
from app.services.standing import FULL, NEW, PAUSED, WARM, ensure_not_paused, standing_for
from app.services.users import set_account_stance
from app.utils.votes import ensure_established_voter
from tests.conftest import seed_user


@pytest.fixture
def warmup_on(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "governance_min_account_age_hours", 168)
    monkeypatch.setattr(settings, "governance_min_meaningful_actions", 3)
    monkeypatch.setattr(settings, "governance_bootstrap_markers", 1)
    monkeypatch.setattr(settings, "governance_min_bot_marks", 2)


def _warm_user(db: Session, prefix: str):
    user_id, _name = seed_user(db, username_prefix=prefix)
    db.execute(
        update(users)
        .where(users.c.id == user_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=10))
    )
    for _ in range(3):
        db.execute(
            insert(meaningful_actions).values(
                id=uuid4(),
                user_id=user_id,
                action_type="create-comment",
                occurred_at=datetime.now(UTC),
                metadata={},
            )
        )
    return user_id


def _founder(db: Session):
    user_id = _warm_user(db, "standing-founder")
    db.execute(
        update(users)
        .where(users.c.id == user_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=100_000))
    )
    return user_id


def _action(db: Session, user_id, *, days_ago: int) -> None:
    db.execute(
        insert(meaningful_actions).values(
            id=uuid4(),
            user_id=user_id,
            action_type="create-comment",
            occurred_at=datetime.now(UTC) - timedelta(days=days_ago),
            metadata={},
        )
    )


def _full_member(db: Session, founder, prefix: str):
    user_id = _warm_user(db, prefix)
    _stance(db, founder, user_id, "vouch")
    return user_id


def _project(db: Session, author) -> object:
    now = datetime.now(UTC)
    project_id = uuid4()
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=f"standing-p-{project_id.hex[:8]}",
            title="Standing project",
            description="seed",
            author_id=author,
            project_mode="personal-service",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="Activity",
            location_label="Online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    return project_id


def _clear_population_cache() -> None:
    try:
        from app.cache import get_sync_redis_client

        redis = get_sync_redis_client()
        for key in redis.scan_iter(match="governance:weekly_active*"):
            redis.delete(key)
    except Exception:
        pass


def _stance(db: Session, source, target, stance: str) -> None:
    now = datetime.now(UTC)
    db.execute(
        insert(account_stances).values(
            source_user_id=source,
            target_user_id=target,
            stance=stance,
            created_at=now,
            updated_at=now,
        )
    )


def test_new_account_can_take_part_but_not_vote(db_transaction: Session, warmup_on) -> None:
    _founder(db_transaction)
    user_id, _name = seed_user(db_transaction, username_prefix="standing-new")
    assert standing_for(db_transaction, user_id).level == NEW
    with pytest.raises(HTTPException) as raised:
        ensure_established_voter(db_transaction, user_id)
    assert raised.value.status_code == 403
    assert "Voting opens after 7 days and 3 contributions" in str(raised.value.detail)


def test_warm_account_votes_at_half_weight(db_transaction: Session, warmup_on) -> None:
    _founder(db_transaction)
    user_id = _warm_user(db_transaction, "standing-warm")
    standing = standing_for(db_transaction, user_id)
    assert standing.level == WARM
    assert standing.weight == 0.5
    assert not standing.counts_for_quorum
    ensure_established_voter(db_transaction, user_id)


def test_one_vouch_from_a_full_member_gives_full_standing(
    db_transaction: Session, warmup_on
) -> None:
    founder = _founder(db_transaction)
    user_id = _warm_user(db_transaction, "standing-vouched")
    assert standing_for(db_transaction, founder).level == FULL
    _stance(db_transaction, founder, user_id, "vouch")
    assert standing_for(db_transaction, user_id).level == FULL


def test_a_vouch_from_a_warm_account_does_not_count(db_transaction: Session, warmup_on) -> None:
    _founder(db_transaction)
    bot_a = _warm_user(db_transaction, "standing-bot-a")
    bot_b = _warm_user(db_transaction, "standing-bot-b")
    _stance(db_transaction, bot_a, bot_b, "vouch")
    _stance(db_transaction, bot_b, bot_a, "vouch")
    assert standing_for(db_transaction, bot_a).level == WARM
    assert standing_for(db_transaction, bot_b).level == WARM


def test_a_completed_service_request_gives_full_standing(
    db_transaction: Session, warmup_on
) -> None:
    founder = _founder(db_transaction)
    provider = _warm_user(db_transaction, "standing-provider")
    project_id = _project(db_transaction, founder)
    now = datetime.now(UTC)
    activity_id = uuid4()
    db_transaction.execute(
        insert(project_activities).values(
            id=activity_id,
            project_id=project_id,
            title="Bike repair",
            author_id=provider,
            scheduled_at=now - timedelta(days=2),
            ends_at=now - timedelta(days=2, hours=-1),
            location_label="Shed",
            note="seed",
        )
    )
    role_id = uuid4()
    db_transaction.execute(
        insert(project_activity_roles).values(
            id=role_id, activity_id=activity_id, label="Provider", required_count=1
        )
    )
    db_transaction.execute(
        insert(project_activity_assignments).values(role_id=role_id, user_id=provider)
    )
    assert standing_for(db_transaction, provider).level == WARM
    db_transaction.execute(
        insert(project_service_history_completions).values(
            id=uuid4(),
            project_id=project_id,
            history_item_key=str(activity_id),
            requester_user_id=founder,
            participant_user_id=None,
            role="requester",
            completion_state="completed",
        )
    )
    assert standing_for(db_transaction, provider).level == FULL


def test_steady_activity_over_a_month_gives_full_standing(
    db_transaction: Session, warmup_on
) -> None:
    _founder(db_transaction)
    user_id = _warm_user(db_transaction, "standing-steady")
    db_transaction.execute(
        update(users)
        .where(users.c.id == user_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=40))
    )
    # Warm-up actions are today (day 1); eight more days makes nine.
    for day in range(8):
        _action(db_transaction, user_id, days_ago=day + 1)
    assert standing_for(db_transaction, user_id).level == WARM
    _action(db_transaction, user_id, days_ago=20)
    assert standing_for(db_transaction, user_id).level == FULL


def test_bot_marks_open_a_review_instead_of_pausing(db_transaction: Session, warmup_on) -> None:
    founder = _founder(db_transaction)
    markers = [_full_member(db_transaction, founder, f"standing-marker-{i}") for i in range(3)]
    suspect = _warm_user(db_transaction, "standing-suspect")
    for marker in markers:
        _stance(db_transaction, marker, suspect, "bot")
    maybe_open_review(db_transaction, suspect)
    assert standing_for(db_transaction, suspect).level == WARM
    review = review_payload(db_transaction, suspect, markers[0])
    assert review is not None and review["status"] == "open"
    assert review["viewer_can_vote"] is True


def test_upheld_review_pauses_and_pauses_vouching(
    db_transaction: Session, warmup_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    founder = _founder(db_transaction)
    voters = [_full_member(db_transaction, founder, f"standing-voter-{i}") for i in range(3)]
    voucher = _full_member(db_transaction, founder, "standing-voucher")
    suspect = _warm_user(db_transaction, "standing-bot")
    _stance(db_transaction, voucher, suspect, "vouch")
    for marker in voters:
        _stance(db_transaction, marker, suspect, "bot")
    maybe_open_review(db_transaction, suspect)
    _clear_population_cache()
    for voter in [founder, voucher, *voters]:
        if (review_payload(db_transaction, suspect, voter) or {}).get("status") == "open":
            cast_vote(db_transaction, voter, suspect, "bot")
    assert standing_for(db_transaction, suspect).level == PAUSED
    with pytest.raises(HTTPException):
        ensure_not_paused(db_transaction, suspect)
    assert vouch_banned_until(db_transaction, voucher) is not None


def test_cleared_review_removes_marks_and_cools_markers(
    db_transaction: Session, warmup_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    founder = _founder(db_transaction)
    markers = [_full_member(db_transaction, founder, f"standing-mk-{i}") for i in range(3)]
    person = _warm_user(db_transaction, "standing-person")
    for marker in markers:
        _stance(db_transaction, marker, person, "bot")
    maybe_open_review(db_transaction, person)
    _clear_population_cache()
    for voter in [founder, *markers]:
        if (review_payload(db_transaction, person, voter) or {}).get("status") == "open":
            cast_vote(db_transaction, voter, person, "real")
    assert standing_for(db_transaction, person).level == WARM
    assert marker_on_cooldown(db_transaction, markers[0], person)


def test_only_full_members_can_vouch_or_mark(
    db_transaction: Session, warmup_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    founder = _founder(db_transaction)
    actor = _warm_user(db_transaction, "standing-actor")
    _target, target_name = seed_user(db_transaction, username_prefix="standing-target")
    for stance in ("vouch", "bot"):
        with pytest.raises(HTTPException) as raised:
            set_account_stance(db_transaction, actor, target_name, stance)
        assert raised.value.status_code == 403
    payload = set_account_stance(db_transaction, founder, target_name, "vouch")
    assert payload["viewer_stance"] == "vouch"


def test_warm_swarm_cannot_pass_a_vote_alone(db_transaction: Session, warmup_on) -> None:
    founder = _founder(db_transaction)
    now = datetime.now(UTC)
    project_id = uuid4()
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=f"standing-q-{project_id.hex[:8]}",
            title="Swarm project",
            description="seed",
            author_id=founder,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="Proposal",
            location_label="Online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    from app.models import project_phase_change_requests

    request_id = uuid4()
    db_transaction.execute(
        insert(project_phase_change_requests).values(
            id=request_id,
            project_id=project_id,
            from_phase_id="phase-1",
            target_phase_id="phase-2",
            change_kind="advance",
            reason="seed",
            author_id=founder,
        )
    )
    swarm = [_warm_user(db_transaction, f"standing-swarm-{index}") for index in range(4)]
    for voter in swarm:
        db_transaction.execute(
            insert(project_phase_change_votes).values(
                request_id=request_id, voter_id=voter, vote="yes"
            )
        )
    summary = compute_vote_summary(db_transaction, project_phase_change_votes, request_id, 4)
    assert summary["yes_count"] == 4
    assert summary["meets_quorum"] is False
    assert summary["is_passing"] is False


def test_new_account_cannot_add_support_but_can_take_it_back(
    db_transaction: Session, warmup_on
) -> None:
    import asyncio

    from app.models import project_signals
    from app.services.projects.actions import toggle_project_signal

    founder = _founder(db_transaction)
    project_id = _project(db_transaction, founder)
    slug = db_transaction.execute(
        select(projects.c.slug).where(projects.c.id == project_id)
    ).scalar_one()
    user_id, _name = seed_user(db_transaction, username_prefix="standing-signal")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(toggle_project_signal(db_transaction, None, user_id, slug, "demand"))
    assert raised.value.status_code == 403

    # A signal from before the rule can still be withdrawn.
    db_transaction.execute(
        insert(project_signals).values(project_id=project_id, user_id=user_id, signal_type="demand")
    )
    removed = asyncio.run(toggle_project_signal(db_transaction, None, user_id, slug, "demand"))
    assert removed["action"] == "removed"
