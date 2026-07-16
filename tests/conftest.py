from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert

from app.db import SessionLocal
from app.models import channels, scope_memberships, users


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def future_scheduled_at(*, hours: int = 1) -> tuple[str, str]:
    now = datetime.now(UTC)
    start = now + timedelta(hours=hours)
    end = now + timedelta(hours=hours + 2)
    return start.isoformat(), end.isoformat()


def seed_user(
    db,
    *,
    username_prefix: str = "test-user",
) -> tuple[UUID, str]:
    user_id = uuid4()
    username = f"{username_prefix}-{str(user_id)[:8]}"
    now = datetime.now(UTC)
    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    return user_id, username


def seed_channel_with_membership(
    db,
    *,
    creator_id: UUID,
    channel_name: str = "Test Channel",
) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    channel_id = uuid4()
    channel_slug = f"ch-{str(channel_id)[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name=channel_name,
            description="seed",
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="channel",
            scope_id=channel_id,
            user_id=creator_id,
            role="member",
            created_at=now,
        )
    )
    return channel_id, channel_slug
