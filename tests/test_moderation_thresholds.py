from __future__ import annotations

from app.services.moderation.serialize import advance_resolution, next_resolution
from app.services.moderation.thresholds import (
    base_quorum,
    confirming_quorum,
    delete_quorum,
    delete_yes_share,
    deletion_ready,
    hide_quorum,
    hide_ready,
    hide_yes_share,
    removed_placeholder,
    required_yes_share,
)
from app.utils.votes import required_votes


def test_minimum_share_never_below_66() -> None:
    assert delete_yes_share("spam", age_days=0, engagement_score=0) == 0.66
    assert delete_yes_share("serious-harm", age_days=0, engagement_score=0) == 0.66
    assert hide_yes_share("serious-harm", age_days=0, engagement_score=0) == 0.66


def test_spam_harder_than_serious_harm_for_old_content() -> None:
    spam = delete_yes_share("spam", age_days=200, engagement_score=60)
    harm = delete_yes_share("serious-harm", age_days=200, engagement_score=60)
    hide = hide_yes_share("serious-harm", age_days=200, engagement_score=60)
    assert spam > harm
    assert harm >= hide
    assert spam <= 0.90
    assert harm <= 0.85
    assert hide <= 0.80


def test_base_quorum_uses_governance_formula() -> None:
    assert base_quorum(1) == required_votes(1)
    assert base_quorum(3) == required_votes(3)
    assert base_quorum(10) == required_votes(10)
    assert base_quorum(1000) == required_votes(1000)


def test_delete_quorum_scales_and_serious_harm_is_lower() -> None:
    spam_q = delete_quorum(100, reason="spam")
    harm_q = delete_quorum(100, reason="serious-harm")
    hide_q = hide_quorum(100)
    assert spam_q == required_votes(100)
    assert harm_q < spam_q
    assert hide_q < harm_q
    assert hide_q >= 3
    assert harm_q >= 5


def test_tiny_dm_can_use_one_vote_quorum() -> None:
    assert delete_quorum(1, reason="spam", target_type="message") == 1
    assert delete_quorum(1, reason="serious-harm", target_type="message") == 1
    assert hide_quorum(1, target_type="message") == 1
    # Non-DM surfaces never get the 1-vote fast path, even with tiny audience.
    assert delete_quorum(1, reason="spam", target_type="comment") >= 3
    assert delete_quorum(1, reason="spam", target_type="post") >= 3
    assert delete_quorum(0, reason="spam", target_type="thread") >= 3
    assert delete_quorum(1, reason="spam", target_type="project") >= 3
    assert delete_quorum(1, reason="spam", target_type="event") >= 3
    assert delete_quorum(1, reason="spam", target_type="help_request") >= 3
    assert delete_quorum(40, reason="spam", target_type="post") > 1
    assert hide_quorum(1, target_type="comment") >= 3


def test_confirming_quorum_alias() -> None:
    assert confirming_quorum(40) == delete_quorum(40, reason="spam")


def test_deletion_ready_requires_quorum_and_ratio() -> None:
    assert not deletion_ready(yes_count=13, no_count=6, delete_quorum_value=20, delete_share=0.66)
    assert deletion_ready(yes_count=14, no_count=6, delete_quorum_value=20, delete_share=0.66)
    assert not deletion_ready(yes_count=22, no_count=8, delete_quorum_value=20, delete_share=0.75)


def test_hide_ready_only_for_serious_harm() -> None:
    assert not hide_ready(
        reason="spam",
        yes_count=5,
        no_count=0,
        hide_quorum_value=3,
        hide_share=0.66,
    )
    assert hide_ready(
        reason="serious-harm",
        yes_count=5,
        no_count=0,
        hide_quorum_value=3,
        hide_share=0.66,
    )


def test_first_report_goes_straight_to_under_review() -> None:
    assert (
        next_resolution(
            target_type="project",
            reason="spam",
            current="open",
            yes_count=1,
            no_count=0,
            eligible=100,
            votes_required=50,
            confirming_required=50,
            delete_yes_share_value=0.66,
        )
        == "under_review"
    )


def test_advance_under_review_then_removed() -> None:
    assert (
        advance_resolution(
            target_type="project",
            reason="spam",
            current="open",
            yes_count=3,
            no_count=0,
            eligible=100,
            votes_required=50,
            confirming_required=50,
            delete_yes_share_value=0.66,
        )
        == "under_review"
    )
    assert (
        advance_resolution(
            target_type="project",
            reason="spam",
            current="under_review",
            yes_count=40,
            no_count=10,
            eligible=100,
            votes_required=50,
            confirming_required=50,
            delete_yes_share_value=0.66,
        )
        == "removed"
    )


def test_tiny_dm_can_complete_in_one_advance() -> None:
    assert (
        advance_resolution(
            target_type="message",
            reason="spam",
            current="open",
            yes_count=1,
            no_count=0,
            eligible=1,
            votes_required=1,
            confirming_required=1,
            delete_yes_share_value=0.66,
        )
        == "removed"
    )


def test_tiny_comment_does_not_instant_delete() -> None:
    assert (
        advance_resolution(
            target_type="comment",
            reason="spam",
            current="open",
            yes_count=1,
            no_count=0,
            eligible=1,
            votes_required=1,
            confirming_required=1,
            delete_yes_share_value=0.66,
        )
        == "under_review"
    )


def test_serious_harm_restricts_then_removes() -> None:
    assert (
        advance_resolution(
            target_type="post",
            reason="serious-harm",
            current="open",
            yes_count=5,
            no_count=0,
            eligible=40,
            votes_required=20,
            confirming_required=20,
            restriction_votes_required=5,
            delete_yes_share_value=0.66,
            hide_yes_share_value=0.66,
        )
        == "hidden"
    )
    assert (
        advance_resolution(
            target_type="post",
            reason="serious-harm",
            current="hidden",
            yes_count=20,
            no_count=5,
            eligible=40,
            votes_required=20,
            confirming_required=20,
            restriction_votes_required=5,
            delete_yes_share_value=0.66,
            hide_yes_share_value=0.66,
        )
        == "removed"
    )


def test_removed_placeholder() -> None:
    assert removed_placeholder("spam") == "Removed for spam"
    assert removed_placeholder("serious-harm") == "Removed for serious harm"


def test_dismiss_when_unreachable() -> None:
    assert (
        next_resolution(
            target_type="post",
            reason="spam",
            current="under_review",
            yes_count=1,
            no_count=10,
            eligible=12,
            votes_required=8,
            confirming_required=8,
            delete_yes_share_value=0.66,
        )
        == "dismissed"
    )


def test_required_yes_share_alias() -> None:
    assert required_yes_share("spam", age_days=0, engagement_score=0) == 0.66
