from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    comments,
    content_votes,
    events,
    platform_board_memberships,
    posts,
    project_memberships,
    projects,
    report_votes,
    reports,
    threads,
    users,
)
from app.services.meaningful_actions import record_meaningful_action
from app.utils.votes import required_votes

COMMENTABLE_SUBJECT_TYPES = frozenset({"thread", "post", "event", "project"})
VOTABLE_TARGET_TYPES = frozenset({"thread", "post", "comment", "event", "project"})
REPORTABLE_TARGET_TYPES = frozenset({"project", "thread", "post", "comment"})
REPORT_REASONS = frozenset({"spam", "serious-harm"})
REPORT_VOTES = frozenset({"yes", "no"})
VOTE_DIRECTIONS = {"up": 1, "down": -1, "neutral": 0}


def _serialize_report(row: Mapping[str, object], vote_summary: dict[str, object]) -> dict[str, object]:
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
        "vote_summary": vote_summary,
    }


def _resolve_target_author_id(db: Session, target_type: str, target_id: UUID) -> UUID | None:
    if target_type == "project":
        row = db.execute(select(projects.c.author_id).where(projects.c.id == target_id)).first()
    elif target_type == "thread":
        row = db.execute(select(threads.c.author_id).where(threads.c.id == target_id)).first()
    elif target_type == "post":
        row = db.execute(select(posts.c.author_id).where(posts.c.id == target_id)).first()
    else:
        row = db.execute(select(comments.c.author_id).where(comments.c.id == target_id)).first()
    if row is None:
        return None
    return row[0]


def _ensure_report_target_exists(db: Session, target_type: str, target_id: UUID) -> None:
    if target_type == "project":
        exists = db.execute(select(projects.c.id).where(projects.c.id == target_id)).first()
    elif target_type == "thread":
        exists = db.execute(select(threads.c.id).where(threads.c.id == target_id)).first()
    elif target_type == "post":
        exists = db.execute(select(posts.c.id).where(posts.c.id == target_id)).first()
    elif target_type == "comment":
        exists = db.execute(select(comments.c.id).where(comments.c.id == target_id)).first()
    else:
        exists = None

    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{target_type.capitalize()} not found")


def _report_vote_summary(db: Session, report_id: UUID, current_user_id: UUID | None = None) -> dict[str, object]:
    rows = db.execute(
        select(report_votes.c.vote, report_votes.c.voter_id).where(report_votes.c.report_id == report_id)
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

    member_count = db.execute(
        select(platform_board_memberships.c.user_id).where(platform_board_memberships.c.standing_state == "member")
    ).all()
    eligible = len(member_count) if len(member_count) > 0 else 1

    return {
        "yes_count": yes_count,
        "no_count": no_count,
        "active_vote": active_vote,
        "eligible_voter_count": eligible,
        "votes_required": required_votes(eligible),
    }


def _serialize_comment(row: Mapping[str, object], replies: list[dict[str, object]] | None = None, active_vote: int = 0) -> dict[str, object]:
    return {
        "id": row["id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "parent_id": row["parent_id"],
        "author_id": row["author_id"],
        "author_username": row.get("author_username", "") or "",
        "body": row["body"],
        "vote_count": row["vote_count"],
        "active_vote": active_vote,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "replies": replies or [],
    }


def _ensure_subject_exists(db: Session, subject_type: str, subject_id: UUID) -> None:
    if subject_type == "thread":
        exists = db.execute(select(threads.c.id).where(threads.c.id == subject_id)).first()
    elif subject_type == "post":
        exists = db.execute(select(posts.c.id).where(posts.c.id == subject_id)).first()
    elif subject_type == "event":
        exists = db.execute(select(events.c.id).where(events.c.id == subject_id)).first()
    elif subject_type == "project":
        exists = db.execute(select(projects.c.id).where(projects.c.id == subject_id)).first()
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid subject_type")

    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{subject_type.capitalize()} not found")


def _ensure_vote_target_exists(db: Session, target_type: str, target_id: UUID) -> None:
    if target_type == "thread":
        exists = db.execute(select(threads.c.id).where(threads.c.id == target_id)).first()
    elif target_type == "post":
        exists = db.execute(select(posts.c.id).where(posts.c.id == target_id)).first()
    elif target_type == "comment":
        exists = db.execute(select(comments.c.id).where(comments.c.id == target_id)).first()
    elif target_type == "event":
        exists = db.execute(select(events.c.id).where(events.c.id == target_id)).first()
    elif target_type == "project":
        exists = db.execute(select(projects.c.id).where(projects.c.id == target_id)).first()
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid target_type")

    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{target_type.capitalize()} not found")


def _update_subject_comment_count(db: Session, subject_type: str, subject_id: UUID, delta: int) -> None:
    try:
        if subject_type == "thread":
            db.execute(
                update(threads)
                .where(threads.c.id == subject_id)
                .values(comment_count=threads.c.comment_count + delta)
            )
        elif subject_type == "event":
            db.execute(
                update(events)
                .where(events.c.id == subject_id)
                .values(comment_count=events.c.comment_count + delta)
            )
        elif subject_type == "project":
            db.execute(
                update(projects)
                .where(projects.c.id == subject_id)
                .values(comment_count=projects.c.comment_count + delta)
            )
        else:
            db.execute(
                update(posts)
                .where(posts.c.id == subject_id)
                .values(comment_count=posts.c.comment_count + delta)
            )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update comment count") from exc


def _apply_vote_count_delta(db: Session, target_type: str, target_id: UUID, delta: int) -> None:
    if delta == 0:
        return

    try:
        if target_type == "thread":
            db.execute(
                update(threads)
                .where(threads.c.id == target_id)
                .values(vote_count=threads.c.vote_count + delta)
            )
        elif target_type == "post":
            db.execute(
                update(posts)
                .where(posts.c.id == target_id)
                .values(vote_count=posts.c.vote_count + delta)
            )
        elif target_type == "event":
            db.execute(
                update(events)
                .where(events.c.id == target_id)
                .values(vote_count=events.c.vote_count + delta)
            )
        elif target_type == "project":
            db.execute(
                update(projects)
                .where(projects.c.id == target_id)
                .values(vote_count=projects.c.vote_count + delta)
            )
        else:
            db.execute(
                update(comments)
                .where(comments.c.id == target_id)
                .values(vote_count=comments.c.vote_count + delta)
            )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update vote count") from exc


def add_comment(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
    body: str,
    parent_id: UUID | None = None,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in COMMENTABLE_SUBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject_type must be one of: {sorted(COMMENTABLE_SUBJECT_TYPES)}",
        )

    _ensure_subject_exists(db, normalized_subject_type, subject_id)

    if parent_id is not None:
        parent = db.execute(select(comments).where(comments.c.id == parent_id)).mappings().first()
        if parent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent comment not found")
        if parent["subject_type"] != normalized_subject_type or parent["subject_id"] != subject_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Parent comment must belong to the same subject",
            )

    try:
        created = db.execute(
            insert(comments)
            .values(
                subject_type=normalized_subject_type,
                subject_id=subject_id,
                parent_id=parent_id,
                author_id=current_user_id,
                body=body.strip(),
            )
            .returning(
                comments.c.id,
                comments.c.subject_type,
                comments.c.subject_id,
                comments.c.parent_id,
                comments.c.author_id,
                comments.c.body,
                comments.c.vote_count,
                comments.c.created_at,
                comments.c.updated_at,
            )
        ).mappings().one()

        _update_subject_comment_count(db, normalized_subject_type, subject_id, 1)
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-comment",
            metadata={
                "subject_type": normalized_subject_type,
                "subject_id": str(subject_id),
                "comment_id": str(created["id"]),
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not add comment") from exc

    author_row = db.execute(
        select(users.c.username).where(users.c.id == current_user_id).limit(1)
    ).first()
    created_with_username = dict(created)
    created_with_username["author_username"] = author_row[0] if author_row else ""

    return {"comment": _serialize_comment(created_with_username)}


def get_comments(
    db: Session,
    subject_type: str,
    subject_id: UUID,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in COMMENTABLE_SUBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject_type must be one of: {sorted(COMMENTABLE_SUBJECT_TYPES)}",
        )

    _ensure_subject_exists(db, normalized_subject_type, subject_id)

    rows = db.execute(
        select(comments, users.c.username.label("author_username"))
        .select_from(comments.outerjoin(users, users.c.id == comments.c.author_id))
        .where(
            comments.c.subject_type == normalized_subject_type,
            comments.c.subject_id == subject_id,
        )
        .order_by(comments.c.created_at.asc())
    ).mappings().all()

    # Bulk-query the viewer's votes on all comments
    active_votes: dict[UUID, int] = {}
    if current_user_id is not None and rows:
        vote_rows = db.execute(
            select(content_votes.c.target_id, content_votes.c.direction).where(
                content_votes.c.target_type == "comment",
                content_votes.c.target_id.in_([row["id"] for row in rows]),
                content_votes.c.voter_id == current_user_id,
            )
        ).all()
        active_votes = {target_id: int(direction) for target_id, direction in vote_rows}

    top_level: dict[UUID, dict[str, object]] = {}
    all_comments: dict[UUID, dict[str, object]] = {}
    children: dict[UUID | None, list[UUID]] = {}
    ordered_ids: list[UUID] = []

    for row in rows:
        item = _serialize_comment(row, replies=[], active_vote=active_votes.get(row["id"], 0))
        all_comments[row["id"]] = item
        parent_key = row["parent_id"]
        if parent_key not in children:
            children[parent_key] = []
        children[parent_key].append(row["id"])

    def build_tree(node_id: UUID) -> dict[str, object]:
        node = all_comments[node_id]
        for child_id in children.get(node_id, []):
            node["replies"].append(build_tree(child_id))
        return node

    for root_id in children.get(None, []):
        ordered_ids.append(root_id)
        top_level[root_id] = all_comments[root_id]

    items = [build_tree(item_id) for item_id in ordered_ids]
    total = len(rows)

    return {
        "subject_type": normalized_subject_type,
        "subject_id": subject_id,
        "total": total,
        "items": items,
    }


def cast_vote(
    db: Session,
    current_user_id: UUID,
    target_type: str,
    target_id: UUID,
    direction: str,
) -> dict[str, object]:
    normalized_target_type = target_type.strip().lower()
    normalized_direction = direction.strip().lower()

    if normalized_target_type not in VOTABLE_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_type must be one of: {sorted(VOTABLE_TARGET_TYPES)}",
        )
    if normalized_direction not in VOTE_DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"direction must be one of: {sorted(VOTE_DIRECTIONS)}",
        )

    _ensure_vote_target_exists(db, normalized_target_type, target_id)

    new_value = VOTE_DIRECTIONS[normalized_direction]

    existing = db.execute(
        select(content_votes.c.id, content_votes.c.direction)
        .where(
            content_votes.c.target_type == normalized_target_type,
            content_votes.c.target_id == target_id,
            content_votes.c.voter_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    old_value = int(existing["direction"]) if existing is not None else 0
    delta = new_value - old_value

    try:
        if new_value == 0:
            if existing is not None:
                db.execute(delete(content_votes).where(content_votes.c.id == existing["id"]))
        elif existing is None:
            db.execute(
                insert(content_votes).values(
                    target_type=normalized_target_type,
                    target_id=target_id,
                    voter_id=current_user_id,
                    direction=new_value,
                )
            )
        else:
            db.execute(
                update(content_votes)
                .where(content_votes.c.id == existing["id"])
                .values(direction=new_value)
            )

        _apply_vote_count_delta(db, normalized_target_type, target_id, delta)
        if new_value != 0:
            record_meaningful_action(
                db=db,
                user_id=current_user_id,
                action_type="cast-vote",
                metadata={
                    "target_type": normalized_target_type,
                    "target_id": str(target_id),
                    "direction": normalized_direction,
                },
            )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast vote") from exc

    return {
        "target_type": normalized_target_type,
        "target_id": target_id,
        "direction": normalized_direction,
        "value": new_value,
    }


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

    existing = db.execute(
        select(reports).where(reports.c.target_type == normalized_target, reports.c.target_id == target_id)
    ).mappings().first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Report already exists for target")

    try:
        created = db.execute(
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
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not submit report") from exc

    summary = _report_vote_summary(db, created["id"], current_user_id)
    return {"report": _serialize_report(created, summary)}


def vote_report(
    db: Session,
    current_user_id: UUID,
    report_id: UUID,
    vote: str,
) -> dict[str, object]:
    normalized_vote = vote.strip().lower()
    if normalized_vote not in REPORT_VOTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"vote must be one of: {sorted(REPORT_VOTES)}",
        )

    row = db.execute(select(reports).where(reports.c.id == report_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    existing = db.execute(
        select(report_votes.c.vote)
        .where(report_votes.c.report_id == report_id, report_votes.c.voter_id == current_user_id)
    ).first()

    try:
        if existing is None:
            db.execute(
                insert(report_votes).values(report_id=report_id, voter_id=current_user_id, vote=normalized_vote)
            )
        else:
            db.execute(
                update(report_votes)
                .where(report_votes.c.report_id == report_id, report_votes.c.voter_id == current_user_id)
                .values(vote=normalized_vote)
            )

        summary = _report_vote_summary(db, report_id, current_user_id)
        yes_count = int(summary["yes_count"])
        no_count = int(summary["no_count"])
        total = yes_count + no_count
        approval = (yes_count / total) if total > 0 else 0.0

        new_resolution = row["resolution"]
        if total >= summary["votes_required"] and approval >= 0.66:
            new_resolution = "hidden"

        db.execute(
            update(reports)
            .where(reports.c.id == report_id)
            .values(resolution=new_resolution)
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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on report") from exc

    refreshed = db.execute(select(reports).where(reports.c.id == report_id)).mappings().one()
    final_summary = _report_vote_summary(db, report_id, current_user_id)
    return {"report": _serialize_report(refreshed, final_summary), "vote": normalized_vote}
