from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    content_votes,
    posts,
    users,
)
from app.services.access_control import (
    can_view_post,
)
from app.services.content.threads import _attach_usernames_to_comments
from app.services.governance import get_comments
from app.services.meaningful_actions import record_meaningful_action

VALID_AUDIENCE = frozenset({"public", "followers"})


def _serialize_post(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "author_id": row["author_id"],
        "body": row["body"],
        "audience": row["audience"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_post(
    db: Session,
    current_user_id: UUID,
    body: str,
    audience: str,
) -> dict[str, object]:
    normalized_audience = audience.strip().lower()
    if normalized_audience not in VALID_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"audience must be one of: {sorted(VALID_AUDIENCE)}",
        )

    try:
        post_row = (
            db.execute(
                insert(posts)
                .values(
                    author_id=current_user_id,
                    body=body.strip(),
                    audience=normalized_audience,
                )
                .returning(
                    posts.c.id,
                    posts.c.author_id,
                    posts.c.body,
                    posts.c.audience,
                    posts.c.vote_count,
                    posts.c.comment_count,
                    posts.c.created_at,
                    posts.c.updated_at,
                )
            )
            .mappings()
            .one()
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-post",
            metadata={"post_id": str(post_row["id"]), "audience": normalized_audience},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create post"
        ) from exc

    return {"post": _serialize_post(post_row)}


def get_post_by_id(
    db: Session, post_id: UUID, current_user_id: UUID | None = None
) -> dict[str, object]:
    row = (
        db.execute(
            select(
                posts.c.id,
                posts.c.author_id,
                posts.c.body,
                posts.c.audience,
                posts.c.vote_count,
                posts.c.comment_count,
                posts.c.created_at,
                posts.c.updated_at,
                users.c.username.label("author_username"),
                users.c.profile_image_url.label("author_profile_image_url"),
            )
            .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
            .where(posts.c.id == post_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    if not can_view_post(db, current_user_id, row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "post",
                content_votes.c.target_id == row["id"],
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    comments_result = get_comments(
        db, subject_type="post", subject_id=row["id"], current_user_id=current_user_id
    )
    discussion = _attach_usernames_to_comments(db, comments_result["items"])

    return {
        "post": {
            "id": row["id"],
            "author_id": row["author_id"],
            "author_username": row["author_username"] or "",
            "author_profile_image_url": row["author_profile_image_url"],
            "body": row["body"],
            "audience": row["audience"],
            "vote_count": row["vote_count"],
            "comment_count": row["comment_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "active_vote": active_vote,
            "discussion": discussion,
        }
    }
