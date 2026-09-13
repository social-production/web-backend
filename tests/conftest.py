from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth.cookies import CSRF_COOKIE
from app.db import SessionLocal, engine
from app.dependencies import get_db
from app.main import app
from app.models import channels, events, projects, scope_memberships, users


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_transaction() -> Generator[Session, None, None]:
    """Rollback all writes after the test — prevents polluting the dev database."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, autocommit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def isolated_client(db_transaction: Session) -> Generator[TestClient, None, None]:
    """TestClient that shares a single rolled-back database transaction."""

    def override_get_db() -> Generator[Session, None, None]:
        yield db_transaction

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def set_project_phase(db: Session, slug: str, phase_id: str, *, commit: bool = False) -> None:
    result = db.execute(
        update(projects)
        .where(projects.c.slug == slug)
        .values(current_phase_id=phase_id, updated_at=datetime.now(UTC))
    )
    if result.rowcount != 1:
        raise AssertionError(f"could not set project {slug} to {phase_id}")
    db.flush()
    if commit:
        db.commit()


def set_event_phase(db: Session, slug: str, phase_id: str, *, commit: bool = False) -> None:
    result = db.execute(
        update(events)
        .where(events.c.slug == slug)
        .values(current_phase_id=phase_id, updated_at=datetime.now(UTC))
    )
    if result.rowcount != 1:
        raise AssertionError(f"could not set event {slug} to {phase_id}")
    db.flush()
    if commit:
        db.commit()


def commit_project_phase(slug: str, phase_id: str) -> None:
    db = SessionLocal()
    try:
        set_project_phase(db, slug, phase_id, commit=True)
    finally:
        db.close()


def commit_event_phase(slug: str, phase_id: str) -> None:
    db = SessionLocal()
    try:
        set_event_phase(db, slug, phase_id, commit=True)
    finally:
        db.close()


def future_scheduled_at(*, hours: int = 1) -> tuple[str, str]:
    now = datetime.now(UTC)
    start = now + timedelta(hours=hours)
    end = now + timedelta(hours=hours + 2)
    return start.isoformat(), end.isoformat()


def private_event_plan_fields(*, hours: int = 24, title: str = "Seeded plan") -> dict[str, object]:
    """Required plan payload for private event creation (starts in Activity)."""
    start = datetime.now(UTC) + timedelta(hours=hours)
    end = start + timedelta(hours=3)
    start_date = start.date().isoformat()
    return {
        "plan_title": title,
        "plan_description": f"{title} description",
        "schedule_payload": {
            "mode": "date",
            "startDate": start_date,
            "startTimeLabel": "18:00",
            "finishTimeLabel": "21:00",
            "startAtUtc": start.isoformat(),
            "endAtUtc": end.isoformat(),
        },
        "plan_payload": {
            "planPhases": [
                {"title": "Main stage", "details": "Run the event activities."},
            ]
        },
    }


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


def seed_scope_membership(
    db,
    *,
    scope_kind: str,
    scope_id: UUID,
    user_id: UUID,
    role: str = "member",
) -> None:
    now = datetime.now(UTC)
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind=scope_kind,
            scope_id=scope_id,
            user_id=user_id,
            role=role,
            created_at=now,
        )
    )


def seed_project(
    db,
    *,
    creator_id: UUID,
    channel_id: UUID | None = None,
    title: str = "Test Project",
) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    project_id = uuid4()
    slug = f"proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title=title,
            description="seed project",
            created_by=creator_id,
            channel_id=channel_id,
            project_mode="standard",
            current_phase_id="phase-1",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    return project_id, slug


def seed_event(
    db,
    *,
    creator_id: UUID,
    community_id: UUID | None = None,
    title: str = "Test Event",
) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    event_id = uuid4()
    slug = f"evt-{str(event_id)[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=slug,
            title=title,
            description="seed event",
            created_by=creator_id,
            community_id=community_id,
            current_phase_id="proposal",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    return event_id, slug


def register_and_login_client(
    client: TestClient,
    *,
    username: str,
    password: str = "password-123",
    ip: str = "10.200.0.1",
) -> dict[str, str]:
    headers = {"X-Forwarded-For": ip, "X-Include-Tokens": "true"}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    response = client.post(
        "/auth/login", json={"username": username, "password": password}, headers=headers
    )
    assert response.status_code == 200, response.text
    return {
        "username": username,
        "access_token": response.json()["access_token"],
        "csrf": client.cookies.get(CSRF_COOKIE, ""),
        "headers": headers,
    }
