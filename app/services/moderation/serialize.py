from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import report_votes, reports, users
from app.services.moderation.electorates import electorate_size_for_target, load_target_engagement
from app.services.moderation.thresholds import (
    ACTIVE_REPORT_RESOLUTIONS,
    can_still_reach_deletion,
    delete_quorum,
    delete_yes_share,
    deletion_ready,
    hide_quorum,
    hide_ready,
    hide_yes_share,
    is_tiny_private_dm,
    removed_placeholder,
)


def _iso(value: object | None) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


def _vote_summary_payload(vote_summary: Mapping[str, object]) -> dict[str, object]:
    return {
        "yes_count": int(vote_summary["yes_count"]),
        "no_count": int(vote_summary["no_count"]),
        "active_vote": vote_summary.get("active_vote"),
        "eligible_voter_count": int(vote_summary["eligible_voter_count"]),
        "audience_size": int(vote_summary["audience_size"]),
        "total_votes": int(vote_summary["total_votes"]),
        "votes_required": int(vote_summary["votes_required"]),
        "required_yes_share": float(vote_summary["required_yes_share"]),
        "delete_yes_share": float(vote_summary["delete_yes_share"]),
        "hide_yes_share": float(vote_summary["hide_yes_share"]),
        "delete_quorum": int(vote_summary["delete_quorum"]),
        "hide_quorum": int(vote_summary["hide_quorum"]),
        "removal_quorum": int(vote_summary["delete_quorum"]),
        "restriction_quorum": int(vote_summary["hide_quorum"]),
        "restriction_votes_required": int(vote_summary["hide_quorum"]),
        "confirming_votes_required": int(vote_summary["delete_quorum"]),
    }


def _frontend_vote_summary(vote_summary: Mapping[str, object]) -> dict[str, object]:
    return {
        "yesCount": int(vote_summary["yes_count"]),
        "noCount": int(vote_summary["no_count"]),
        "activeVote": vote_summary.get("active_vote"),
        "eligibleVoterCount": int(vote_summary["eligible_voter_count"]),
        "audienceSize": int(vote_summary["audience_size"]),
        "totalVotes": int(vote_summary["total_votes"]),
        "votesRequired": int(vote_summary["votes_required"]),
        "requiredYesShare": float(vote_summary["required_yes_share"]),
        "deleteYesShare": float(vote_summary["delete_yes_share"]),
        "hideYesShare": float(vote_summary["hide_yes_share"]),
        "deleteQuorum": int(vote_summary["delete_quorum"]),
        "hideQuorum": int(vote_summary["hide_quorum"]),
        "removalQuorum": int(vote_summary["delete_quorum"]),
        "restrictionQuorum": int(vote_summary["hide_quorum"]),
        "restrictionVotesRequired": int(vote_summary["hide_quorum"]),
    }


def compute_vote_summary(
    db: Session,
    *,
    report_id: UUID,
    target_type: str,
    target_id: UUID,
    reported_author_id: UUID | None,
    reason: str,
    created_at: object | None,
    engagement_score: int,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    rows = db.execute(
        select(report_votes.c.vote, report_votes.c.voter_id).where(
            report_votes.c.report_id == report_id
        )
    ).all()

    yes_count = 0
    no_count = 0
    active_vote = None
    for vote, voter_id in rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1
        if current_user_id is not None and voter_id == current_user_id:
            active_vote = vote

    audience_size = electorate_size_for_target(
        db,
        target_type=target_type,
        target_id=target_id,
        reported_author_id=reported_author_id,
    )

    age_days = 0.0
    if created_at is not None and hasattr(created_at, "astimezone"):
        age_days = max(
            0.0,
            (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds() / 86400.0,  # type: ignore[union-attr]
        )

    delete_share = delete_yes_share(reason, age_days=age_days, engagement_score=engagement_score)
    hide_share = hide_yes_share(reason, age_days=age_days, engagement_score=engagement_score)
    dq = delete_quorum(audience_size, reason=reason, target_type=target_type)
    hq = hide_quorum(audience_size, target_type=target_type)
    total_votes = yes_count + no_count

    return {
        "yes_count": yes_count,
        "no_count": no_count,
        "active_vote": active_vote,
        "eligible_voter_count": audience_size,
        "audience_size": audience_size,
        "total_votes": total_votes,
        "votes_required": dq,
        "required_yes_share": delete_share,
        "delete_yes_share": delete_share,
        "hide_yes_share": hide_share,
        "delete_quorum": dq,
        "hide_quorum": hq,
        "removal_quorum": dq,
        "restriction_quorum": hq,
        "restriction_votes_required": hq,
        "confirming_votes_required": dq,
    }


def serialize_report_row(
    row: Mapping[str, object],
    vote_summary: Mapping[str, object],
    *,
    author_username: str = "",
) -> dict[str, object]:
    return {
        "id": row["id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "reason": row["reason"],
        "description": row["description"],
        "reporter_id": row["reporter_id"],
        "reported_author_id": row["reported_author_id"],
        "resolution": row["resolution"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "vote_summary": _vote_summary_payload(vote_summary),
        "authorUsername": author_username,
        "createdAt": _iso(row["created_at"]),
        "voteSummary": _frontend_vote_summary(vote_summary),
    }


def frontend_report_summary(
    row: Mapping[str, object],
    vote_summary: Mapping[str, object],
    *,
    author_username: str = "",
) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "subjectId": str(row["subject_id"]),
        "targetId": str(row["target_id"]),
        "reason": row["reason"],
        "description": row["description"],
        "createdAt": _iso(row["created_at"]),
        "authorUsername": author_username,
        "resolution": row["resolution"],
        "voteSummary": _frontend_vote_summary(vote_summary),
    }


def load_active_report(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    current_user_id: UUID | None = None,
    engagement_score: int | None = None,
    created_at: object | None = None,
) -> dict[str, object] | None:
    row = (
        db.execute(
            select(reports).where(
                reports.c.target_type == target_type,
                reports.c.target_id == target_id,
                reports.c.resolution.in_(tuple(ACTIVE_REPORT_RESOLUTIONS)),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    username = ""
    if row["reporter_id"] is not None:
        user_row = db.execute(
            select(users.c.username).where(users.c.id == row["reporter_id"])
        ).first()
        username = user_row[0] if user_row else ""

    target_created_at, _vote_count, loaded_score = load_target_engagement(
        db, target_type=str(row["target_type"]), target_id=row["target_id"]
    )
    score = loaded_score if engagement_score is None else engagement_score

    summary = compute_vote_summary(
        db,
        report_id=row["id"],
        target_type=str(row["target_type"]),
        target_id=row["target_id"],
        reported_author_id=row["reported_author_id"],
        reason=str(row["reason"]),
        created_at=created_at or target_created_at or row["created_at"],
        engagement_score=score,
        current_user_id=current_user_id,
    )
    return frontend_report_summary(row, summary, author_username=username)


def load_active_reports_for_targets(
    db: Session,
    *,
    target_type: str,
    target_ids: Sequence[UUID],
    current_user_id: UUID | None = None,
) -> dict[UUID, dict[str, object]]:
    if not target_ids:
        return {}

    rows = (
        db.execute(
            select(reports).where(
                reports.c.target_type == target_type,
                reports.c.target_id.in_(list(target_ids)),
                reports.c.resolution.in_(tuple(ACTIVE_REPORT_RESOLUTIONS)),
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return {}

    reporter_ids = {row["reporter_id"] for row in rows if row["reporter_id"] is not None}
    usernames: dict[UUID, str] = {}
    if reporter_ids:
        for user_id, username in db.execute(
            select(users.c.id, users.c.username).where(users.c.id.in_(list(reporter_ids)))
        ).all():
            usernames[user_id] = username

    result: dict[UUID, dict[str, object]] = {}
    for row in rows:
        target_created_at, _vote_count, engagement_score = load_target_engagement(
            db, target_type=str(row["target_type"]), target_id=row["target_id"]
        )
        summary = compute_vote_summary(
            db,
            report_id=row["id"],
            target_type=str(row["target_type"]),
            target_id=row["target_id"],
            reported_author_id=row["reported_author_id"],
            reason=str(row["reason"]),
            created_at=target_created_at or row["created_at"],
            engagement_score=engagement_score,
            current_user_id=current_user_id,
        )
        result[row["target_id"]] = frontend_report_summary(
            row,
            summary,
            author_username=usernames.get(row["reporter_id"], "") if row["reporter_id"] else "",
        )
    return result


def next_resolution(
    *,
    target_type: str,
    reason: str,
    current: str,
    yes_count: int,
    no_count: int,
    eligible: int,
    votes_required: int,
    confirming_required: int,
    restriction_votes_required: int | None = None,
    delete_yes_share_value: float | None = None,
    hide_yes_share_value: float | None = None,
) -> str:
    """Advance report state using quorum + yes-ratio rules.

    Lifecycle:
    - first eligible yes vote immediately marks the case ``under_review``
    - serious-harm may later cross a lower hide quorum + hide ratio into ``hidden``
    - final deletion needs delete quorum + delete yes ratio
    - only true 1:1 DMs may delete in one step when eligible <= 1
    """
    delete_share = delete_yes_share_value if delete_yes_share_value is not None else 0.66
    hide_share = hide_yes_share_value if hide_yes_share_value is not None else 0.66
    hide_q = (
        restriction_votes_required
        if restriction_votes_required is not None
        else confirming_required
    )
    delete_q = votes_required

    if current in {"under_review", "hidden"} and not can_still_reach_deletion(
        yes_count=yes_count,
        no_count=no_count,
        eligible=eligible,
        delete_quorum_value=delete_q,
        delete_share=delete_share,
    ):
        return "dismissed"

    ready_to_delete = deletion_ready(
        yes_count=yes_count,
        no_count=no_count,
        delete_quorum_value=delete_q,
        delete_share=delete_share,
    )
    ready_to_hide = hide_ready(
        reason=reason,
        yes_count=yes_count,
        no_count=no_count,
        hide_quorum_value=hide_q,
        hide_share=hide_share,
    )

    if current == "open" and yes_count >= 1:
        if is_tiny_private_dm(target_type=target_type, audience_size=eligible) and ready_to_delete:
            return "removed"
        return "under_review"

    if ready_to_delete and current in {"under_review", "hidden"}:
        return "removed"

    if ready_to_hide and current == "under_review":
        return "hidden"

    return current if current in ACTIVE_REPORT_RESOLUTIONS else "open"


def advance_resolution(
    *,
    target_type: str,
    reason: str,
    current: str,
    yes_count: int,
    no_count: int,
    eligible: int,
    votes_required: int,
    confirming_required: int,
    restriction_votes_required: int | None = None,
    delete_yes_share_value: float | None = None,
    hide_yes_share_value: float | None = None,
) -> str:
    """Resolve through intermediate states in one evaluation.

    From ``open``, always land on ``under_review`` first for non-DM surfaces.
    Serious-harm may continue to ``hidden`` in the same request. Full deletion
    in the same request is reserved for true 1:1 DMs.
    """
    resolution = current if current in ACTIVE_REPORT_RESOLUTIONS else "open"
    nxt = next_resolution(
        target_type=target_type,
        reason=reason,
        current=resolution,
        yes_count=yes_count,
        no_count=no_count,
        eligible=eligible,
        votes_required=votes_required,
        confirming_required=confirming_required,
        restriction_votes_required=restriction_votes_required,
        delete_yes_share_value=delete_yes_share_value,
        hide_yes_share_value=hide_yes_share_value,
    )
    if nxt == resolution or nxt in {"removed", "dismissed"}:
        return nxt

    if nxt == "under_review":
        further = next_resolution(
            target_type=target_type,
            reason=reason,
            current="under_review",
            yes_count=yes_count,
            no_count=no_count,
            eligible=eligible,
            votes_required=votes_required,
            confirming_required=confirming_required,
            restriction_votes_required=restriction_votes_required,
            delete_yes_share_value=delete_yes_share_value,
            hide_yes_share_value=hide_yes_share_value,
        )
        if further == "hidden":
            return "hidden"
        if further == "removed" and is_tiny_private_dm(
            target_type=target_type, audience_size=eligible
        ):
            return "removed"
        return "under_review"

    return nxt


def moderation_body_for_comment(
    *,
    body: str,
    moderation_state: str | None,
    moderation_reason: str | None,
) -> str:
    if moderation_state == "removed":
        return removed_placeholder(moderation_reason)
    return body
