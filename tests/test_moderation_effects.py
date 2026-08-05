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
