from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.people_suggestions import get_ranked_people_suggestions


def search_message_contacts(
    db: Session,
    current_user_id: UUID,
    query: str = "",
    limit: int = 8,
) -> dict[str, object]:
    capped_limit = max(1, min(limit, 25))
    items = get_ranked_people_suggestions(
        db,
        current_user_id,
        query=query,
        limit=capped_limit,
    )

    return {
        "total": len(items),
        "items": items,
    }
