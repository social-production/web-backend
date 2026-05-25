from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models import meaningful_actions


def record_meaningful_action(
    db: Session,
    user_id: UUID,
    action_type: str,
    metadata: dict[str, object] | None = None,
) -> None:
    db.execute(
        insert(meaningful_actions).values(
            user_id=user_id,
            action_type=action_type.strip(),
            occurred_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
    )
