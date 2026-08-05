"""Ranked people suggestions: following → followers → everyone else."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import user_follows, users


def get_ranked_people_suggestions(
    db: Session,
    current_user_id: UUID,
    *,
    query: str | None = None,
    limit: int = 12,
    exclude_ids: set[UUID] | None = None,
) -> list[dict[str, object]]:
    """
    Return people suggestions ranked by relationship closeness:
    1. people the viewer follows
    2. people who follow the viewer
    3. everyone else globally

    Within each tier, results are ordered by username.
    """
    normalized_query = (query or "").strip().lower()
    excluded = set(exclude_ids or set())
    excluded.add(current_user_id)

    following_ids = select(user_follows.c.followed_id).where(
        user_follows.c.follower_id == current_user_id,
        user_follows.c.status == "accepted",
    )
    follower_ids = select(user_follows.c.follower_id).where(
        user_follows.c.followed_id == current_user_id,
        user_follows.c.status == "accepted",
    )

    rank = case(
        (users.c.id.in_(following_ids), 0),
        (users.c.id.in_(follower_ids), 1),
        else_=2,
    )

    statement = select(
        users.c.id,
        users.c.username,
        users.c.bio,
        users.c.profile_image_url,
        rank.label("rank"),
    ).where(users.c.is_active.is_(True))

    if excluded:
        statement = statement.where(users.c.id.notin_(excluded))

    if normalized_query:
        statement = statement.where(
            or_(
                func.lower(users.c.username).like(f"{normalized_query}%"),
                func.lower(users.c.username).like(f"%{normalized_query}%"),
            )
        )

    rows = (
        db.execute(
            statement.order_by(rank.asc(), users.c.username.asc()).limit(max(1, min(limit, 40)))
        )
        .mappings()
        .all()
    )

    return [
        {
            "id": row["id"],
            "username": row["username"],
            "bio": row["bio"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]
