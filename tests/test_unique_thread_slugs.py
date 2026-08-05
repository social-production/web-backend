"""Creating threads with duplicate titles should allocate unique slugs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import channels, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)
    user_id = uuid4()
    username = f"thread-slug-{user_id.hex[:8]}"
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
    channel_id = uuid4()
    channel_slug = f"thread-ch-{channel_id.hex[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Thread Slug Test Channel",
            description="seed",
            created_by=user_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.close()
    return {
        "token": create_access_token(str(user_id)),
        "channel_slug": channel_slug,
    }


def test_duplicate_thread_titles_get_unique_slugs() -> None:
    seeded = _seed()
    title = "Weekly Discussion"
    payload = {
        "title": title,
        "body": "First duplicate title test.",
        "channel_slugs": [seeded["channel_slug"]],
    }

    with TestClient(app) as client:
        first = client.post("/content/threads", headers=_auth_header(seeded["token"]), json=payload)
        assert first.status_code == 200, first.text
        first_slug = first.json()["thread"]["slug"]

        second = client.post(
            "/content/threads",
            headers=_auth_header(seeded["token"]),
            json={
                **payload,
                "body": "Second duplicate title test.",
            },
        )
        assert second.status_code == 200, second.text
        second_slug = second.json()["thread"]["slug"]

    assert first_slug != second_slug
    assert first_slug.startswith("weekly-discussion-")
    assert second_slug.startswith("weekly-discussion-")
