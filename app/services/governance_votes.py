from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils.votes import required_votes

APPROVAL_THRESHOLD = 0.66


def compute_vote_summary(
    db: Session,
    vote_table,
    request_id: UUID,
    member_count: int,
    *,
    approval_threshold: float = APPROVAL_THRESHOLD,
) -> dict[str, object]:
    rows = db.execute(select(vote_table.c.vote).where(vote_table.c.request_id == request_id)).all()

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
