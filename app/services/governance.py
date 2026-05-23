from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import comments, content_votes, posts, threads

COMMENTABLE_SUBJECT_TYPES = frozenset({"thread", "post"})
VOTABLE_TARGET_TYPES = frozenset({"thread", "post", "comment"})
VOTE_DIRECTIONS = {"up": 1, "down": -1, "neutral": 0}


def _serialize_comment(row: Mapping[str, object], replies: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "id": row["id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "parent_id": row["parent_id"],
        "author_id": row["author_id"],
        "body": row["body"],
        "vote_count": row["vote_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "replies": replies or [],
    }


def _ensure_subject_exists(db: Session, subject_type: str, subject_id: UUID) -> None:
    if subject_type == "thread":
        exists = db.execute(select(threads.c.id).where(threads.c.id == subject_id)).first()
    elif subject_type == "post":
        exists = db.execute(select(posts.c.id).where(posts.c.id == subject_id)).first()
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
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid target_type")

    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{target_type.capitalize()} not found")


def _update_subject_comment_count(db: Session, subject_type: str, subject_id: UUID, delta: int) -> None:
    if subject_type == "thread":
        db.execute(
            update(threads)
            .where(threads.c.id == subject_id)
            .values(comment_count=threads.c.comment_count + delta)
        )
    else:
        db.execute(
            update(posts)
            .where(posts.c.id == subject_id)
            .values(comment_count=posts.c.comment_count + delta)
        )


def _apply_vote_count_delta(db: Session, target_type: str, target_id: UUID, delta: int) -> None:
    if delta == 0:
        return

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
    else:
        db.execute(
            update(comments)
            .where(comments.c.id == target_id)
            .values(vote_count=comments.c.vote_count + delta)
        )


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
        if parent["parent_id"] is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Comments support only one level of replies",
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
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not add comment") from exc

    return {"comment": _serialize_comment(created)}


def get_comments(
    db: Session,
    subject_type: str,
    subject_id: UUID,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in COMMENTABLE_SUBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject_type must be one of: {sorted(COMMENTABLE_SUBJECT_TYPES)}",
        )

    _ensure_subject_exists(db, normalized_subject_type, subject_id)

    rows = db.execute(
        select(comments)
        .where(
            comments.c.subject_type == normalized_subject_type,
            comments.c.subject_id == subject_id,
        )
        .order_by(comments.c.created_at.asc())
    ).mappings().all()

    top_level: dict[UUID, dict[str, object]] = {}
    ordered_top_level_ids: list[UUID] = []

    for row in rows:
        if row["parent_id"] is None:
            item = _serialize_comment(row, replies=[])
            top_level[row["id"]] = item
            ordered_top_level_ids.append(row["id"])

    for row in rows:
        parent_id = row["parent_id"]
        if parent_id is None:
            continue
        parent = top_level.get(parent_id)
        if parent is not None:
            parent["replies"].append(_serialize_comment(row))

    items = [top_level[item_id] for item_id in ordered_top_level_ids]
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
