from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.cache import get_sync_redis_client
from app.models import meaningful_actions


WEEKLY_ACTIVE_CACHE_KEY = "board:weekly_active"
GOVERNANCE_WEEKLY_ACTIVE_KEY = "governance:weekly_active"


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
    # Invalidate the weekly active users caches so the next query recomputes.
    try:
        redis = get_sync_redis_client()
        redis.delete(WEEKLY_ACTIVE_CACHE_KEY, GOVERNANCE_WEEKLY_ACTIVE_KEY)
        # Also flush all per-project / per-event governance caches to limit staleness
        cursor: int = 0
        while True:
            cursor, keys = redis.scan(cursor, match="governance:weekly_active:project:*", count=100)
            if keys:
                redis.delete(*keys)
            if cursor == 0:
                break
        cursor = 0
        while True:
            cursor, keys = redis.scan(cursor, match="governance:weekly_active:event:*", count=100)
            if keys:
                redis.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
