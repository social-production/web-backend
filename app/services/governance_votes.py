from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66
SOFTWARE_APPROVAL_THRESHOLD_RATIO = 0.66


def compute_vote_summary(
    db: Session,
    vote_table,
    request_id: UUID,
    member_count: int,
    *,
    approval_threshold: float = APPROVAL_THRESHOLD,
    id_column: str = "request_id",
) -> dict[str, object]:
    id_col = getattr(vote_table.c, id_column)
    rows = db.execute(select(vote_table.c.vote).where(id_col == request_id)).all()

    yes_count = 0
    no_count = 0
    for (vote,) in rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1

    total_votes = yes_count + no_count
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    votes_required = required_votes(member_count)
    meets_quorum = total_votes >= votes_required
    meets_approval = approval_ratio >= approval_threshold
    is_passing = meets_quorum and meets_approval

    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_meet_quorum = max_total >= votes_required
    can_meet_approval = (max_yes / max_total * 100.0) >= (approval_threshold * 100.0) if max_total > 0 else False
    can_still_pass = (not is_passing) and can_meet_quorum and can_meet_approval

    return {
        "yes_count": yes_count,
        "no_count": no_count,
        "total_votes": total_votes,
        "approval_ratio": approval_ratio,
        "approval_threshold": approval_threshold,
        "votes_required": votes_required,
        "member_count": member_count,
        "meets_quorum": meets_quorum,
        "meets_approval": meets_approval,
        "is_passing": is_passing,
        "can_still_pass": can_still_pass,
    }


def compute_plan_vote_summary(
    db: Session,
    vote_table,
    plan_id: UUID,
    member_count: int,
    *,
    winning_key: str = "is_winning",
) -> dict[str, object]:
    summary = compute_vote_summary(
        db,
        vote_table,
        plan_id,
        member_count,
        id_column="plan_id",
    )
    if winning_key != "is_passing":
        summary[winning_key] = summary.pop("is_passing")
    return summary


def compute_software_vote_summary(
    vote_rows: list[Mapping[str, object]],
    member_count: int,
    current_user_id: UUID | None,
    *,
    approval_threshold_ratio: float = SOFTWARE_APPROVAL_THRESHOLD_RATIO,
) -> tuple[dict[str, object], bool, bool]:
    yes_count = 0
    no_count = 0
    active_vote: str | None = None

    for row in vote_rows:
        vote_value = str(row["vote"]).lower()
        if vote_value == "yes":
            yes_count += 1
        elif vote_value == "no":
            no_count += 1

        if current_user_id is not None and row["voter_id"] == current_user_id:
            active_vote = vote_value

    total_votes = yes_count + no_count
    eligible_voter_count = max(member_count, 0)
    votes_required = required_votes(eligible_voter_count)
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    approval_percent = approval_ratio * 100.0
    meets_quorum = total_votes >= votes_required
    passes = meets_quorum and approval_ratio >= approval_threshold_ratio

    remaining_eligible_votes = max(0, eligible_voter_count - total_votes)
    max_yes = yes_count + remaining_eligible_votes
    max_total = total_votes + remaining_eligible_votes
    can_meet_approval = (max_yes / max_total) >= approval_threshold_ratio if max_total > 0 else False
    can_meet_quorum = max_total >= votes_required
    can_still_pass = (not passes) and can_meet_approval and can_meet_quorum

    quorum_threshold_percent = (votes_required / eligible_voter_count * 100.0) if eligible_voter_count > 0 else 0.0

    summary = {
        "yesCount": yes_count,
        "noCount": no_count,
        "totalVotes": total_votes,
        "approvalPercent": approval_percent,
        "activeVote": active_vote,
        "meetsQuorum": meets_quorum,
        "eligibleVoterCount": eligible_voter_count,
        "quorumThresholdPercent": quorum_threshold_percent,
        "approvalThresholdPercent": approval_threshold_ratio * 100.0,
        "votesRequired": votes_required,
        "votesRemaining": max(0, votes_required - total_votes),
        "remainingEligibleVotes": remaining_eligible_votes,
        "canStillPass": can_still_pass,
    }
    return summary, passes, can_still_pass
