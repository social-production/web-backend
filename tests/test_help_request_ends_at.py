from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models import channels, users


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _create_user(username: str) -> str:
    db = SessionLocal()
    user_id = uuid4()
    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash=hash_password("password-123"),
            bio=username,
            is_active=True,
        )
    )
    db.commit()
    db.close()
    return str(user_id)


def _create_channel(owner_id: str) -> str:
    db = SessionLocal()
    channel_id = uuid4()
    channel_slug = _unique("help-ch")
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name=channel_slug,
            description="seed",
            created_by=owner_id,
        )
    )
    db.commit()
    db.close()
    return channel_slug


def _auth_headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def test_help_request_create_with_end_time(client: TestClient):
    owner = _unique("help-end")
    owner_id = _create_user(owner)
    channel_slug = _create_channel(owner_id)
    needed_at = datetime.now(UTC) + timedelta(hours=2)
    ends_at = needed_at + timedelta(hours=1)

    created = client.post(
        "/content/help-requests",
        headers=_auth_headers(owner_id),
        json={
            "title": "Need a hand",
            "body": "Moving furniture across town",
            "location_label": "Online",
            "needed_at": needed_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "roles": [{"title": "Helper", "description": "Carry boxes", "slots": 1}],
            "channel_slugs": [channel_slug],
            "community_slugs": [],
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()["help_request"]
    assert payload["ends_at"] is not None
    assert "–" in payload["schedule_label"]

    fetched = client.get(
        f"/content/help-requests/{payload['id']}",
        headers=_auth_headers(owner_id),
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["help_request"]["ends_at"] is not None


def test_help_request_rejects_end_before_start(client: TestClient):
    owner = _unique("help-bad")
    owner_id = _create_user(owner)
    channel_slug = _create_channel(owner_id)
    needed_at = datetime.now(UTC) + timedelta(hours=2)
    ends_at = needed_at - timedelta(minutes=15)

    created = client.post(
        "/content/help-requests",
        headers=_auth_headers(owner_id),
        json={
            "title": "Bad schedule",
            "body": "End before start",
            "location_label": "Online",
            "needed_at": needed_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "roles": [{"title": "Helper", "description": "Nope", "slots": 1}],
            "channel_slugs": [channel_slug],
            "community_slugs": [],
        },
    )
    assert created.status_code == 422, created.text
