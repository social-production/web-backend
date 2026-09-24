from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    comments,
    events,
    help_requests,
    messages,
    posts,
    projects,
    report_votes,
    reports,
    threads,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.moderation.effects import apply_resolution_effect
from app.services.moderation.electorates import (
    load_target_engagement,
    viewer_can_vote_on_target,
)
from app.services.moderation.serialize import (
    advance_resolution as resolve_next,
)
from app.services.moderation.serialize import (
    compute_vote_summary,
    serialize_report_row,
)
from app.services.moderation.thresholds import REPORT_REASONS, REPORTABLE_TARGET_TYPES


def _resolve_target_author_id(db: Session, target_type: str, target_id: UUID) -> UUID | None:
    if target_type == "message":
        row = db.execute(select(messages.c.sender_id).where(messages.c.id == target_id)).first()
        return row[0] if row else None
    if target_type == "event":
        row = db.execute(select(events.c.created_by).where(events.c.id == target_id)).first()
        return row[0] if row else None

    table_map = {
        "project": projects,
        "thread": threads,
        "post": posts,
        "help_request": help_requests,
        "comment": comments,
    }
    table = table_map.get(target_type)
    if table is None:
        return None
    row = db.execute(select(table.c.author_id).where(table.c.id == target_id)).first()
    return row[0] if row else None


def _ensure_report_target_exists(db: Session, target_type: str, target_id: UUID) -> None:
    table_map = {
        "project": projects,
        "thread": threads,
        "post": posts,
        "help_request": help_requests,
        "event": events,
        "comment": comments,
        "message": messages,
    }
    table = table_map.get(target_type)
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid target_type"
        )
    exists = db.execute(select(table.c.id).where(table.c.id == target_id)).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{target_type.capitalize()} not found"
        )


def submit_report(
    db: Session,
    current_user_id: UUID,
    target_type: str,
    target_id: UUID,
    reason: str,
    description: str,
) -> dict[str, object]:
    normalized_target = target_type.strip().lower()
    normalized_reason = reason.strip().lower()
    if normalized_target not in REPORTABLE_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_type must be one of: {sorted(REPORTABLE_TARGET_TYPES)}",
        )
    if normalized_reason not in REPORT_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"reason must be one of: {sorted(REPORT_REASONS)}",
        )

    _ensure_report_target_exists(db, normalized_target, target_id)
    reported_author_id = _resolve_target_author_id(db, normalized_target, target_id)
    if reported_author_id is not None and reported_author_id == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You can't report yourself"
        )

    created_at, _vote_count, engagement_score = load_target_engagement(
        db, target_type=normalized_target, target_id=target_id
    )

    try:
        created = (
            db.execute(
                insert(reports)
                .values(
                    subject_type=normalized_target,
                    subject_id=target_id,
                    target_type=normalized_target,
                    target_id=target_id,
                    reason=normalized_reason,
                    description=description.strip(),
                    reporter_id=current_user_id,
                    reported_author_id=reported_author_id,
                    resolution="open",
                )
                .returning(
                    reports.c.id,
                    reports.c.subject_type,
                    reports.c.subject_id,
                    reports.c.target_type,
                    reports.c.target_id,
                    reports.c.reason,
                    reports.c.description,
                    reports.c.reporter_id,
                    reports.c.reported_author_id,
                    reports.c.resolution,
                    reports.c.created_at,
                    reports.c.updated_at,
                )
            )
            .mappings()
            .one()
        )

        if viewer_can_vote_on_target(
            db,
            viewer_id=current_user_id,
            target_type=normalized_target,
            target_id=target_id,
            reported_author_id=reported_author_id,
        ):
            db.execute(
                insert(report_votes).values(
                    report_id=created["id"], voter_id=current_user_id, vote="yes"
                )
            )

        summary = compute_vote_summary(
            db,
            report_id=created["id"],
            target_type=normalized_target,
            target_id=target_id,
            reported_author_id=reported_author_id,
            reason=normalized_reason,
            created_at=created_at or created["created_at"],
            engagement_score=engagement_score,
            current_user_id=current_user_id,
        )
        resolution = resolve_next(
            target_type=normalized_target,
            reason=normalized_reason,
            current="open",
            yes_count=int(summary["yes_count"]),
            no_count=int(summary["no_count"]),
            eligible=int(summary["audience_size"]),
            votes_required=int(summary["delete_quorum"]),
            confirming_required=int(summary["delete_quorum"]),
            restriction_votes_required=int(summary["hide_quorum"]),
            delete_yes_share_value=float(summary["delete_yes_share"]),
            hide_yes_share_value=float(summary["hide_yes_share"]),
        )
        if resolution != "open":
            db.execute(
                update(reports).where(reports.c.id == created["id"]).values(resolution=resolution)
            )
            apply_resolution_effect(
                db,
                target_type=normalized_target,
                target_id=target_id,
                reason=normalized_reason,
                resolution=resolution,
            )
            created = dict(created)
            created["resolution"] = resolution

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Report already exists for target"
        ) from exc

    refreshed = db.execute(select(reports).where(reports.c.id == created["id"])).mappings().one()
    summary = compute_vote_summary(
        db,
        report_id=refreshed["id"],
        target_type=normalized_target,
        target_id=target_id,
        reported_author_id=reported_author_id,
        reason=normalized_reason,
        created_at=created_at or refreshed["created_at"],
        engagement_score=engagement_score,
        current_user_id=current_user_id,
    )
    return {"report": serialize_report_row(refreshed, summary)}


def vote_report(
    db: Session,
    current_user_id: UUID,
    report_id: UUID,
    vote: str,
) -> dict[str, object]:
    normalized_vote = vote.strip().lower()
    if normalized_vote not in {"yes", "no"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be one of: yes, no"
        )

    row = db.execute(select(reports).where(reports.c.id == report_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if row["resolution"] in {"removed", "dismissed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Report is already resolved"
        )

    if not viewer_can_vote_on_target(
        db,
        viewer_id=current_user_id,
        target_type=str(row["target_type"]),
        target_id=row["target_id"],
        reported_author_id=row["reported_author_id"],
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not eligible to vote on this report"
        )

    created_at, _vote_count, engagement_score = load_target_engagement(
        db, target_type=str(row["target_type"]), target_id=row["target_id"]
    )

    existing = db.execute(
        select(report_votes.c.vote).where(
            report_votes.c.report_id == report_id, report_votes.c.voter_id == current_user_id
        )
    ).first()

    try:
        if existing is not None and existing[0] == normalized_vote:
            db.commit()
            refreshed = (
                db.execute(select(reports).where(reports.c.id == report_id)).mappings().one()
            )
            final_summary = compute_vote_summary(
                db,
                report_id=report_id,
                target_type=str(row["target_type"]),
                target_id=row["target_id"],
                reported_author_id=row["reported_author_id"],
                reason=str(row["reason"]),
                created_at=created_at or row["created_at"],
                engagement_score=engagement_score,
                current_user_id=current_user_id,
            )
            return {
                "report": serialize_report_row(refreshed, final_summary),
                "vote": normalized_vote,
            }

        if normalized_vote == "neutral":
            if existing is not None:
                db.execute(
                    delete(report_votes).where(
                    report_votes.c.report_id == report_id,
                    report_votes.c.voter_id == current_user_id,
                    )
                )
        elif existing is None:
            db.execute(
                insert(report_votes).values(
                    report_id=report_id, voter_id=current_user_id, vote=normalized_vote
                )
            )
        else:
            db.execute(
                update(report_votes)
                .where(
                    report_votes.c.report_id == report_id,
                    report_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        summary = compute_vote_summary(
            db,
            report_id=report_id,
            target_type=str(row["target_type"]),
            target_id=row["target_id"],
            reported_author_id=row["reported_author_id"],
            reason=str(row["reason"]),
            created_at=created_at or row["created_at"],
            engagement_score=engagement_score,
            current_user_id=current_user_id,
        )
        new_resolution = resolve_next(
            target_type=str(row["target_type"]),
            reason=str(row["reason"]),
            current=str(row["resolution"]),
            yes_count=int(summary["yes_count"]),
            no_count=int(summary["no_count"]),
            eligible=int(summary["audience_size"]),
            votes_required=int(summary["delete_quorum"]),
            confirming_required=int(summary["delete_quorum"]),
            restriction_votes_required=int(summary["hide_quorum"]),
            delete_yes_share_value=float(summary["delete_yes_share"]),
            hide_yes_share_value=float(summary["hide_yes_share"]),
        )

        db.execute(
            update(reports).where(reports.c.id == report_id).values(resolution=new_resolution)
        )
        apply_resolution_effect(
            db,
            target_type=str(row["target_type"]),
            target_id=row["target_id"],
            reason=str(row["reason"]),
            resolution=new_resolution,
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "report",
                "target_id": str(report_id),
                "vote": normalized_vote,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on report"
        ) from exc

    refreshed = db.execute(select(reports).where(reports.c.id == report_id)).mappings().one()
    final_summary = compute_vote_summary(
        db,
        report_id=report_id,
        target_type=str(row["target_type"]),
        target_id=row["target_id"],
        reported_author_id=row["reported_author_id"],
        reason=str(row["reason"]),
        created_at=created_at or row["created_at"],
        engagement_score=engagement_score,
        current_user_id=current_user_id,
    )
    return {"report": serialize_report_row(refreshed, final_summary), "vote": normalized_vote}
