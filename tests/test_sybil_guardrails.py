from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    event_memberships,
    events,
    meaningful_actions,
    project_activities,
    project_activity_roles,
    project_memberships,
    projects,
    users,
)
from app.services.board import volunteer_as_candidate
from app.services.moderation.reports import record_content_removal_penalty
from app.services.projects.actions import commit_project_activity_role
from app.utils.votes import (
    CONTENT_REMOVED_ACTION,
    WARMUP_DETAIL,
    ensure_established_voter,
    is_established_voter,
    weekly_active_event_members,
    weekly_active_project_members,
    weekly_active_users_global,
)
from tests.conftest import seed_user

pytestmark = pytest.mark.usefixtures("db_transaction")


def _enable_warmup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    age: int = 24,
    actions: int = 3,
    penalty: int = 168,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "governance_min_account_age_hours", age)
    monkeypatch.setattr(settings, "governance_min_meaningful_actions", actions)
    monkeypatch.setattr(settings, "governance_removal_penalty_hours", penalty)


def _age_account(db: Session, user_id, *, hours: int) -> None:
    db.execute(
        update(users)
        .where(users.c.id == user_id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
    )


def _record(db: Session, user_id, action_type: str, *, when: datetime | None = None) -> None:
    db.execute(
        insert(meaningful_actions).values(
            id=uuid4(),
            user_id=user_id,
            action_type=action_type,
            occurred_at=when or datetime.now(UTC),
            metadata={},
        )
    )


def _clear_population_cache() -> None:
    try:
        from app.cache import get_sync_redis_client

        redis = get_sync_redis_client()
        redis.delete("governance:weekly_active")
        cursor = 0
        while True:
            cursor, keys = redis.scan(cursor, match="governance:weekly_active:*", count=200)
            if keys:
                redis.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass


def test_new_account_is_refused_when_warmup_is_on(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch)
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-new")
    _record(db_transaction, user_id, "create-comment")
    assert is_established_voter(db_transaction, user_id) is False
    with pytest.raises(HTTPException) as raised:
        ensure_established_voter(db_transaction, user_id)
    assert raised.value.status_code == 403
    assert raised.value.detail == WARMUP_DETAIL


def test_old_account_with_counted_actions_is_accepted(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch, actions=2)
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-ready")
    _age_account(db_transaction, user_id, hours=48)
    _record(db_transaction, user_id, "create-comment")
    _record(db_transaction, user_id, "create-post")
    assert is_established_voter(db_transaction, user_id) is True
    ensure_established_voter(db_transaction, user_id)


def test_join_only_does_not_establish(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch, actions=1)
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-join")
    _age_account(db_transaction, user_id, hours=240)
    _record(db_transaction, user_id, "join-project")
    _record(db_transaction, user_id, "join-event")
    _record(db_transaction, user_id, "follow-user")
    assert is_established_voter(db_transaction, user_id) is False


def test_content_removal_pushes_warmup_forward(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch, actions=1, penalty=168)
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-penalty")
    _age_account(db_transaction, user_id, hours=240)
    _record(db_transaction, user_id, "create-comment")
    assert is_established_voter(db_transaction, user_id) is True

    target_id = uuid4()
    record_content_removal_penalty(
        db_transaction,
        previous_resolution="open",
        new_resolution="removed",
        reported_author_id=user_id,
        target_type="comment",
        target_id=target_id,
        reason="spam",
    )
    assert is_established_voter(db_transaction, user_id) is False
    recorded = db_transaction.execute(
        select(func.count())
        .select_from(meaningful_actions)
        .where(
            meaningful_actions.c.user_id == user_id,
            meaningful_actions.c.action_type == CONTENT_REMOVED_ACTION,
        )
    ).scalar_one()
    assert int(recorded) == 1

    record_content_removal_penalty(
        db_transaction,
        previous_resolution="hidden",
        new_resolution="removed",
        reported_author_id=user_id,
        target_type="comment",
        target_id=target_id,
        reason="spam",
    )
    recorded_again = db_transaction.execute(
        select(func.count())
        .select_from(meaningful_actions)
        .where(
            meaningful_actions.c.user_id == user_id,
            meaningful_actions.c.action_type == CONTENT_REMOVED_ACTION,
        )
    ).scalar_one()
    assert int(recorded_again) == 1


def test_quorum_excludes_unestablished_accounts(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch, actions=1)
    now = datetime.now(UTC)
    ready_id, _ready_name = seed_user(db_transaction, username_prefix="sybil-quorum-ready")
    fresh_id, _fresh_name = seed_user(db_transaction, username_prefix="sybil-quorum-fresh")
    _age_account(db_transaction, ready_id, hours=240)

    project_id = uuid4()
    slug = f"sybil-proj-{project_id.hex[:8]}"
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Sybil quorum project",
            description="seed",
            author_id=ready_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="activity",
            location_label="online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    for user_id in (ready_id, fresh_id):
        db_transaction.execute(
            insert(project_memberships).values(
                project_id=project_id,
                user_id=user_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
            )
        )

    event_id = uuid4()
    db_transaction.execute(
        insert(events).values(
            id=event_id,
            slug=f"sybil-evt-{event_id.hex[:8]}",
            title="Sybil quorum event",
            description="seed",
            created_by=ready_id,
            current_phase_id="activity",
            time_label="Soon",
            location_label="Hall",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    for user_id in (ready_id, fresh_id):
        db_transaction.execute(
            insert(event_memberships).values(
                event_id=event_id,
                user_id=user_id,
                role="member",
                joined_at=now,
            )
        )

    _clear_population_cache()
    assert weekly_active_project_members(db_transaction, project_id) == 0
    assert weekly_active_event_members(db_transaction, event_id) == 0
    before_global = weekly_active_users_global(db_transaction)

    _record(db_transaction, ready_id, "create-comment")
    _record(db_transaction, fresh_id, "join-project")
    _clear_population_cache()
    assert weekly_active_project_members(db_transaction, project_id) == 1
    assert weekly_active_event_members(db_transaction, event_id) == 1
    assert weekly_active_users_global(db_transaction) == before_global + 1


def test_thresholds_at_zero_leave_population_and_votes_open(db_transaction: Session) -> None:
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-open")
    assert is_established_voter(db_transaction, user_id) is True
    ensure_established_voter(db_transaction, user_id)
    _record(db_transaction, user_id, "join-project")
    now = datetime.now(UTC)
    project_id = uuid4()
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=f"sybil-open-{project_id.hex[:8]}",
            title="Open quorum project",
            description="seed",
            author_id=user_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="activity",
            location_label="online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db_transaction.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=user_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    _clear_population_cache()
    assert weekly_active_project_members(db_transaction, project_id) == 1


def test_new_account_cannot_volunteer_but_can_take_an_activity_role(
    db_transaction: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_warmup(monkeypatch, actions=1)
    user_id, _username = seed_user(db_transaction, username_prefix="sybil-role")
    now = datetime.now(UTC)
    project_id = uuid4()
    slug = f"sybil-role-{project_id.hex[:8]}"
    activity_id = uuid4()
    role_id = uuid4()
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Sybil role project",
            description="seed",
            author_id=user_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="activity",
            location_label="online",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db_transaction.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=user_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    db_transaction.execute(
        insert(project_activities).values(
            id=activity_id,
            project_id=project_id,
            title="Shift",
            author_id=user_id,
            scheduled_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=2),
            location_label="Hall",
            note="help",
            created_at=now,
            updated_at=now,
        )
    )
    db_transaction.execute(
        insert(project_activity_roles).values(
            id=role_id,
            activity_id=activity_id,
            label="Cook",
            required_count=1,
            maximum_count=2,
            created_at=now,
        )
    )

    with pytest.raises(HTTPException) as raised:
        volunteer_as_candidate(db_transaction, user_id)
    assert raised.value.status_code == 403
    assert raised.value.detail == WARMUP_DETAIL

    monkeypatch.setattr(Session, "commit", lambda self: self.flush())
    result = commit_project_activity_role(
        db_transaction,
        user_id,
        slug,
        activity_id,
        role_label="Cook",
    )
    assert result["ok"] is True
    assert result["role_id"] == role_id
