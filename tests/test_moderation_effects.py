from __future__ import annotations

from app.services.moderation.effects import apply_resolution_effect
from app.services.moderation.thresholds import REPORTABLE_TARGET_TYPES, removed_placeholder


def test_reportable_types_include_help_request_and_message() -> None:
    assert "help_request" in REPORTABLE_TARGET_TYPES
    assert "message" in REPORTABLE_TARGET_TYPES
    assert "event" in REPORTABLE_TARGET_TYPES


def test_placeholder_copy() -> None:
    assert removed_placeholder("spam") == "Removed for spam"
    assert removed_placeholder("serious-harm") == "Removed for serious harm"


def test_apply_resolution_effect_removed_sets_state(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_apply(db, *, target_type, target_id, moderation_state, moderation_reason):
        calls.append((target_type, target_id, moderation_state, moderation_reason))

    unindex_calls: list[tuple] = []

    def fake_unindex(db, *, target_type, target_id):
        unindex_calls.append((target_type, target_id))

    monkeypatch.setattr(
        "app.services.moderation.effects.apply_moderation_state",
        fake_apply,
    )
    monkeypatch.setattr(
        "app.services.moderation.effects._unindex_search_document",
        fake_unindex,
    )

    from uuid import uuid4

    target_id = uuid4()
    apply_resolution_effect(
        None,  # type: ignore[arg-type]
        target_type="help_request",
        target_id=target_id,
        reason="spam",
        resolution="removed",
    )
    assert calls == [("help_request", target_id, "removed", "spam")]
    assert unindex_calls == [("help_request", target_id)]

    calls.clear()
    unindex_calls.clear()
    apply_resolution_effect(
        None,  # type: ignore[arg-type]
        target_type="comment",
        target_id=target_id,
        reason="serious-harm",
        resolution="hidden",
    )
    assert calls == [("comment", target_id, "hidden", "serious-harm")]
    assert unindex_calls == []


def test_apply_resolution_effect_under_review_for_comment_spam(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_apply(db, *, target_type, target_id, moderation_state, moderation_reason):
        calls.append((target_type, target_id, moderation_state, moderation_reason))

    monkeypatch.setattr(
        "app.services.moderation.effects.apply_moderation_state",
        fake_apply,
    )

    from uuid import uuid4

    target_id = uuid4()
    apply_resolution_effect(
        None,  # type: ignore[arg-type]
        target_type="comment",
        target_id=target_id,
        reason="spam",
        resolution="under_review",
    )
    assert calls == [("comment", target_id, "under_review", "spam")]


def test_removed_project_ends_members_and_open_votes(db_transaction) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import insert, select

    from app.models import (
        notifications,
        project_memberships,
        project_phase_change_requests,
        projects,
    )
    from tests.conftest import seed_user

    creator_id, _ = seed_user(db_transaction, username_prefix="rm-creator")
    member_id, _ = seed_user(db_transaction, username_prefix="rm-member")
    now = datetime.now(UTC)
    project_id = uuid4()
    db_transaction.execute(
        insert(projects).values(
            id=project_id,
            slug=f"rm-{str(project_id)[:8]}",
            title="Duplicate beach clean",
            description="seed",
            author_id=creator_id,
            project_mode="productive",
            current_phase_id="phase-1",
            stage_label="Proposal",
            location_label="Online",
            last_activity_at=now,
        )
    )
    for user_id in (creator_id, member_id):
        db_transaction.execute(
            insert(project_memberships).values(
                project_id=project_id, user_id=user_id, joined_at=now
            )
        )
    db_transaction.execute(
        insert(project_phase_change_requests).values(
            project_id=project_id,
            author_id=creator_id,
            from_phase_id="phase-1",
            target_phase_id="phase-2",
            change_kind="advance",
            reason="Ready",
            status="open",
        )
    )

    apply_resolution_effect(
        db_transaction,
        target_type="project",
        target_id=project_id,
        reason="spam",
        resolution="removed",
    )

    state = db_transaction.execute(
        select(projects.c.moderation_state).where(projects.c.id == project_id)
    ).scalar_one()
    assert state == "removed"
    assert (
        db_transaction.execute(
            select(project_memberships).where(project_memberships.c.project_id == project_id)
        ).first()
        is None
    )
    statuses = (
        db_transaction.execute(
            select(project_phase_change_requests.c.status).where(
                project_phase_change_requests.c.project_id == project_id
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["closed"]
    notified = set(
        db_transaction.execute(
            select(notifications.c.recipient_id).where(
                notifications.c.kind == "prj-removed",
                notifications.c.subject_id == project_id,
            )
        )
        .scalars()
        .all()
    )
    assert notified == {creator_id, member_id}
