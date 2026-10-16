"""Standing: warm-up, half-weight warm votes, earned full standing, bot pause."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    account_stances,
    meaningful_actions,
    project_activities,
    project_activity_assignments,
    project_activity_ratings,
    project_activity_roles,
    project_phase_change_votes,
    projects,
    users,
)
from app.services.governance_votes import compute_vote_summary
from app.services.standing import FULL, NEW, PAUSED, WARM, standing_for
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


def test_attending_an_activity_someone_else_rated_gives_full_standing(
    db_transaction: Session, warmup_on
) -> None:
    founder = _founder(db_transaction)
    attendee = _warm_user(db_transaction, "standing-attendee")
    now = datetime.now(UTC)
    project_id = uuid4()
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=f"standing-{project_id.hex[:8]}",
            title="Standing project",
            description="seed",
            author_id=founder,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="Activity",
            location_label="Online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    activity_id = uuid4()
    db_transaction.execute(
        insert(project_activities).values(
            id=activity_id,
            project_id=project_id,
            title="Clean-up",
            author_id=founder,
            scheduled_at=now - timedelta(days=2),
            ends_at=now - timedelta(days=2, hours=-3),
            location_label="Beach",
            note="seed",
        )
    )
    role_id = uuid4()
    db_transaction.execute(
        insert(project_activity_roles).values(
            id=role_id, activity_id=activity_id, label="Helper", required_count=1
        )
    )
    db_transaction.execute(
        insert(project_activity_assignments).values(role_id=role_id, user_id=attendee)
    )
    assert standing_for(db_transaction, attendee).level == WARM

    db_transaction.execute(
        insert(project_activity_ratings).values(activity_id=activity_id, user_id=founder, rating=5)
    )
    assert standing_for(db_transaction, attendee).level == FULL


def test_bot_marks_from_full_members_pause_an_account(db_transaction: Session, warmup_on) -> None:
    founder = _founder(db_transaction)
    second = _warm_user(db_transaction, "standing-second")
    _stance(db_transaction, founder, second, "vouch")
    suspect = _warm_user(db_transaction, "standing-suspect")
    _stance(db_transaction, founder, suspect, "bot")
    _stance(db_transaction, second, suspect, "bot")
    assert standing_for(db_transaction, suspect).level == PAUSED
    with pytest.raises(HTTPException):
        ensure_established_voter(db_transaction, suspect)


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
