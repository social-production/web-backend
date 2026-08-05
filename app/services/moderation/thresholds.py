from __future__ import annotations

from math import ceil
from typing import Literal

from app.utils.votes import required_votes

ReportReason = Literal["spam", "serious-harm"]
ReportResolution = Literal["open", "under_review", "hidden", "removed", "dismissed"]
ModerationState = Literal["visible", "under_review", "hidden", "removed"]

TOP_LEVEL_TARGET_TYPES = frozenset({"post", "thread", "project", "event", "help_request"})
MESSAGE_LIKE_TARGET_TYPES = frozenset({"comment", "message"})
REPORTABLE_TARGET_TYPES = TOP_LEVEL_TARGET_TYPES | MESSAGE_LIKE_TARGET_TYPES
REPORT_REASONS = frozenset({"spam", "serious-harm"})
ACTIVE_REPORT_RESOLUTIONS = frozenset({"open", "under_review", "hidden"})
TERMINAL_REPORT_RESOLUTIONS = frozenset({"removed", "dismissed"})

# Floor for non-tiny surfaces so decisions never look like a one-person dogpile.
# 1-vote delete/hide is reserved for true 1:1 DMs only (see is_tiny_private_dm).
MIN_NON_DM_DELETE_QUORUM = 3
MIN_SERIOUS_HARM_HIDE_QUORUM = 3
MIN_SERIOUS_HARM_DELETE_QUORUM = 5
MIN_APPROVAL_SHARE = 0.66


def age_boost_percent(age_days: float) -> int:
    """Stability boost from content age (percentage points above 66%)."""
    if age_days < 1:
        return 0
    if age_days < 7:
        return 10
    if age_days < 30:
        return 20
    if age_days < 180:
        return 30
    return 35


def popularity_boost_percent(score: int) -> int:
    """Stability boost from real participation (percentage points above 66%)."""
    if score < 2:
        return 0
    if score < 8:
        return 5
    if score < 20:
        return 10
    if score < 50:
        return 15
    return 20


# Alias kept for older call sites / tests.
engagement_boost_percent = popularity_boost_percent


def base_quorum(audience_size: int) -> int:
    """Governance required-votes formula on weekly-active hybrid audience N."""
    return max(0, required_votes(max(0, audience_size)))


def is_tiny_private_dm(*, target_type: str, audience_size: int) -> bool:
    """1-vote fast path is only for true 1:1 DMs."""
    return target_type == "message" and audience_size <= 1


def delete_yes_share(reason: str, *, age_days: float, engagement_score: int) -> float:
    """Yes ratio among votes cast required for deletion. Never below 66%."""
    age = age_boost_percent(age_days)
    popularity = popularity_boost_percent(engagement_score)
    if reason == "serious-harm":
        boost = (age + popularity) // 2
        return min(0.85, MIN_APPROVAL_SHARE + boost / 100.0)
    return min(0.90, MIN_APPROVAL_SHARE + (age + popularity) / 100.0)


def hide_yes_share(reason: str, *, age_days: float, engagement_score: int) -> float:
    """Yes ratio among votes cast required for serious-harm blur/hide."""
    if reason != "serious-harm":
        return 1.0
    age = age_boost_percent(age_days)
    popularity = popularity_boost_percent(engagement_score)
    boost = (age + popularity) // 4
    return min(0.80, MIN_APPROVAL_SHARE + boost / 100.0)


def delete_quorum(
    audience_size: int,
    *,
    reason: str,
    target_type: str = "",
) -> int:
    """Minimum total votes cast before deletion can pass."""
    if is_tiny_private_dm(target_type=target_type, audience_size=audience_size):
        return 1

    base = base_quorum(audience_size)
    if reason == "serious-harm":
        if base <= 0:
            return MIN_SERIOUS_HARM_DELETE_QUORUM
        return max(MIN_SERIOUS_HARM_DELETE_QUORUM, ceil(base * 0.66))

    # Spam / other non-DM surfaces never use a 1-vote threshold.
    if base <= 0:
        return MIN_NON_DM_DELETE_QUORUM
    return max(MIN_NON_DM_DELETE_QUORUM, base)


def hide_quorum(audience_size: int, *, target_type: str = "") -> int:
    """Minimum total votes cast before serious-harm blur/hide."""
    if is_tiny_private_dm(target_type=target_type, audience_size=audience_size):
        return 1

    base = base_quorum(audience_size)
    if base <= 0:
        return MIN_SERIOUS_HARM_HIDE_QUORUM
    return max(MIN_SERIOUS_HARM_HIDE_QUORUM, ceil(base * 0.33))


def approval_ratio(yes_count: int, no_count: int) -> float:
    total = yes_count + no_count
    if total <= 0:
        return 0.0
    return yes_count / total


def deletion_ready(
    *,
    yes_count: int,
    no_count: int,
    delete_quorum_value: int,
    delete_share: float,
) -> bool:
    total = yes_count + no_count
    return total >= delete_quorum_value and approval_ratio(yes_count, no_count) >= delete_share


def hide_ready(
    *,
    reason: str,
    yes_count: int,
    no_count: int,
    hide_quorum_value: int,
    hide_share: float,
) -> bool:
    if reason != "serious-harm":
        return False
    total = yes_count + no_count
    return total >= hide_quorum_value and approval_ratio(yes_count, no_count) >= hide_share


def can_still_reach_deletion(
    *,
    yes_count: int,
    no_count: int,
    eligible: int,
    delete_quorum_value: int,
    delete_share: float,
) -> bool:
    total = yes_count + no_count
    remaining = max(0, eligible - total)
    max_total = total + remaining
    max_yes = yes_count + remaining
    if max_total < delete_quorum_value:
        return False
    if max_total <= 0:
        return False
    return (max_yes / max_total) >= delete_share


# ---- Backward-compatible aliases used by older tests/call sites ----


def required_yes_share(reason: str, *, age_days: float, engagement_score: int) -> float:
    return delete_yes_share(reason, age_days=age_days, engagement_score=engagement_score)


def restriction_yes_share(reason: str, *, age_days: float, engagement_score: int) -> float:
    return hide_yes_share(reason, age_days=age_days, engagement_score=engagement_score)


def removal_quorum(eligible: int, *, engagement_score: int = 0, reason: str = "spam") -> int:
    del engagement_score
    return delete_quorum(eligible, reason=reason)


def restriction_quorum(eligible: int, *, engagement_score: int = 0) -> int:
    del engagement_score
    return hide_quorum(eligible)


def confirming_quorum(eligible: int) -> int:
    return delete_quorum(eligible, reason="spam")


def votes_needed_for_share(eligible: int, share: float) -> int:
    """Legacy helper: ceil(eligible * share). Prefer quorum + ratio checks."""
    if eligible <= 0:
        return 1
    return min(eligible, max(1, ceil(eligible * share)))


def votes_required_for_removal(
    eligible: int,
    *,
    reason: str,
    age_days: float,
    engagement_score: int,
    target_type: str = "",
) -> tuple[float, int, int, int]:
    """Return (share, delete_quorum, hide_quorum, votes_required_alias).

    ``votes_required`` is kept as the delete quorum for older callers that still
    treat it as an absolute bar. Pass/fail now also requires the yes-ratio.
    """
    share = delete_yes_share(reason, age_days=age_days, engagement_score=engagement_score)
    dq = delete_quorum(eligible, reason=reason, target_type=target_type)
    hq = hide_quorum(eligible, target_type=target_type)
    return share, dq, hq, dq


def votes_required_for_restriction(
    eligible: int,
    *,
    reason: str,
    age_days: float,
    engagement_score: int,
    target_type: str = "",
) -> tuple[float, int, int, int]:
    share = hide_yes_share(reason, age_days=age_days, engagement_score=engagement_score)
    hq = hide_quorum(eligible, target_type=target_type)
    return share, hq, hq, hq


def reason_label(reason: str) -> str:
    return "spam" if reason == "spam" else "serious harm"


def removed_placeholder(reason: str | None) -> str:
    label = reason_label(reason or "spam")
    return f"Removed for {label}"
