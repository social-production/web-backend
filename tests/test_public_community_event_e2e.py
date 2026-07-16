from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import communities, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    creator_id = uuid4()
    community_id = uuid4()
    creator_name = f"evt-co-{str(creator_id)[:8]}"
    community_slug = f"pub-co-{str(community_id)[:8]}"

    db.execute(
        insert(users).values(
            id=creator_id,
            username=creator_name,
            email=f"{creator_name}@t.invalid",
            password_hash="x",
            bio=creator_name,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=community_slug,
            name="Public Community Event",
            description="seed",
            join_policy="open",
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.close()

    return {
        "creator_token": create_access_token(str(creator_id)),
        "community_slug": community_slug,
    }


def run() -> None:
    seeded = _seed()
    event_slug = f"pub-co-evt-{str(uuid4())[:8]}"

    with TestClient(app) as client:
        created = client.post(
            "/events",
            headers=_auth_header(seeded["creator_token"]),
            json={
                "slug": event_slug,
                "title": "Community Only Event",
                "description": "Public event tagged to a community only.",
                "is_private": False,
                "time_label": "Soon",
                "location_label": "Online",
                "channel_slugs": [],
                "community_slugs": [seeded["community_slug"]],
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["event"]["slug"] == event_slug

        no_tags = client.post(
            "/events",
            headers=_auth_header(seeded["creator_token"]),
            json={
                "slug": f"no-tags-{str(uuid4())[:8]}",
                "title": "No Tags",
                "description": "Should fail.",
                "is_private": False,
                "time_label": "Soon",
                "location_label": "Online",
                "channel_slugs": [],
                "community_slugs": [],
            },
        )
        assert no_tags.status_code == 422, no_tags.text

    print("test_public_community_event_e2e: ok")


if __name__ == "__main__":
    run()
