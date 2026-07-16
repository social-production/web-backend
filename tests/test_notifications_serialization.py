from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.exc import OperationalError

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import notifications, users
from app.services.notifications import _serialize_notification, list_notifications


def test_serialize_notification_emits_iso_datetimes() -> None:
    now = datetime(2026, 7, 5, 5, 51, tzinfo=UTC)
    read_at = datetime(2026, 7, 5, 6, 0, tzinfo=UTC)
    notification_id = uuid4()
    recipient_id = uuid4()
    actor_id = uuid4()
    subject_id = uuid4()

    payload = _serialize_notification(
        {
            "id": notification_id,
            "recipient_id": recipient_id,
            "actor_id": actor_id,
            "actor_username": "alice",
            "kind": "evt-phase-vote",
            "surface": "event",
            "subject_type": "phase-change",
            "subject_id": subject_id,
            "target_id": subject_id,
            "title": "Vote on phase change",
            "body": "A phase change needs your vote.",
            "href": "/events/demo",
            "is_unread": False,
            "created_at": now,
            "read_at": read_at,
        }
    )

    assert payload["created_at"] == now.isoformat()
    assert payload["read_at"] == read_at.isoformat()
    assert payload["kind"] == "evt-phase-vote"


def test_list_notifications_serializes_new_kinds_with_iso_datetimes() -> None:
    try:
        db = SessionLocal()
        db.connection()
    except OperationalError:
        pytest.skip("Postgres not available")

    now = datetime.now(UTC)
    recipient_id = uuid4()
    actor_id = uuid4()
    subject_id = uuid4()

    db.execute(
        insert(users).values(
            id=recipient_id,
            username=f"recipient-{recipient_id.hex[:8]}",
            email=f"{recipient_id.hex[:8]}@t.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=actor_id,
            username=f"actor-{actor_id.hex[:8]}",
            email=f"{actor_id.hex[:8]}@t.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )

    notification_kinds = [
        ("evt-phase-vote", "event", "phase-change"),
        ("prj-share", "project", "project"),
        ("community-invite", "community", "community"),
        ("pr-approved", "project", "pull-request"),
        ("evt-plan-lead", "event", "event-plan"),
    ]

    for kind, surface, subject_type in notification_kinds:
        db.execute(
            insert(notifications).values(
                id=uuid4(),
                recipient_id=recipient_id,
                actor_id=actor_id,
                kind=kind,
                surface=surface,
                subject_type=subject_type,
                subject_id=subject_id,
                target_id=subject_id,
                title=f"Test {kind}",
                body=f"Body for {kind}",
                href="/",
                is_unread=True,
                read_at=None,
                created_at=now,
            )
        )
    db.commit()

    payload = list_notifications(db, recipient_id)
    assert len(payload["items"]) == len(notification_kinds)

    for item in payload["items"]:
        assert isinstance(item["created_at"], str)
        assert item["read_at"] is None
        datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))

    token = create_access_token(str(recipient_id))
    client = TestClient(app)
    response = client.get("/notifications", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(notification_kinds)
    returned_kinds = {item["kind"] for item in body["items"]}
    assert returned_kinds == {kind for kind, _, _ in notification_kinds}

    db.close()
