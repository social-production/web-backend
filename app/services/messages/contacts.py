from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    users,
)


def search_message_contacts(
    db: Session,
    current_user_id: UUID,
    query: str = "",
    limit: int = 8,
) -> dict[str, object]:
    normalized_query = query.strip().lower()
    capped_limit = max(1, min(limit, 25))
    conditions = [
        users.c.is_active.is_(True),
        users.c.id != current_user_id,
    ]
    if normalized_query:
        conditions.append(
            or_(
                users.c.username.ilike(f"%{normalized_query}%"),
            )
        )

    rows = (
        db.execute(
            select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
            .where(*conditions)
            .order_by(users.c.username.asc())
            .limit(capped_limit)
        )
        .mappings()
        .all()
    )

    return {
        "total": len(rows),
        "items": [
            {
                "id": row["id"],
                "username": row["username"],
                "bio": row["bio"],
                "profileImageUrl": row["profile_image_url"],
            }
            for row in rows
        ],
    }
