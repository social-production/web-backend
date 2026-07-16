from __future__ import annotations

import json
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


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(UTC)

    creator_id = uuid4()
    requester_id = uuid4()
    channel_id = uuid4()

    creator_name = f"ps-creator-{str(creator_id)[:8]}"
    requester_name = f"ps-requester-{str(requester_id)[:8]}"
    channel_slug = f"ps-ch-{str(channel_id)[:8]}"

    for user_id, username in [(creator_id, creator_name), (requester_id, requester_name)]:
        db.execute(
            insert(users).values(
                id=user_id,
                username=username,
                email=f"{username}@t.invalid",
                password_hash="x",
                bio=username,
                created_at=now,
                updated_at=now,
            )
        )

    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Personal Service Channel",
            description="seed",
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
    )

    db.commit()
    db.close()

    return {
        "creator_token": create_access_token(str(creator_id)),
        "requester_token": create_access_token(str(requester_id)),
        "channel_slug": channel_slug,
        "creator_name": creator_name,
        "requester_name": requester_name,
    }


def run() -> None:
    seeded = _seed()
    project_slug = f"ps-svc-{str(uuid4())[:8]}"

    with TestClient(app) as client:
        created = client.post(
            "/projects",
            headers=_auth_header(seeded["creator_token"]),
            json={
                "slug": project_slug,
                "title": "Personal Tutoring",
                "description": "One-on-one tutoring sessions.",
                "project_mode": "personal-service",
                "location_label": "Online",
                "channel_slugs": [seeded["channel_slug"]],
                "request_mode": "direct",
            },
        )
        assert created.status_code == 200, created.text

        request_resp = client.post(
            f"/projects/{project_slug}/service-requests",
            headers=_auth_header(seeded["requester_token"]),
            json={
                "title": "Need help with algebra",
                "body": "Can we schedule a one-hour session?",
            },
        )
        assert request_resp.status_code == 200, request_resp.text
        request_payload = request_resp.json()
        request_id = str(request_payload["request"]["id"])
        conversation_id = request_payload.get("conversation_id")
        assert conversation_id, request_payload

        bootstrap = client.get("/bootstrap", headers=_auth_header(seeded["creator_token"]))
        assert bootstrap.status_code == 200, bootstrap.text

        rail = bootstrap.json()["activityRail"]
        request_items = [item for item in rail if item.get("kind") == "request"]
        matching = [
            item
            for item in request_items
            if item.get("projectSlug") == project_slug and item.get("requestId") == request_id
        ]

        assert matching, f"Expected personal-service request in activityRail, got {request_items}"
        assert matching[0]["href"] == f"/projects/{project_slug}?request={request_id}"
        assert matching[0]["conversationId"] == conversation_id

        messages = client.get(
            "/messages/conversations", headers=_auth_header(seeded["creator_token"])
        )
        assert messages.status_code == 200, messages.text
        conversation_ids = [item["id"] for item in messages.json()["items"]]
        assert conversation_id in conversation_ids

        thread = client.get(
            f"/messages/conversations/{conversation_id}/messages",
            headers=_auth_header(seeded["creator_token"]),
        )
        assert thread.status_code == 200, thread.text
        assert any(
            "Need help with algebra" in message["body"] for message in thread.json()["items"]
        )

        print(
            json.dumps(
                {
                    "project_slug": project_slug,
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "rail_request_count": len(request_items),
                    "href": matching[0]["href"],
                }
            )
        )


if __name__ == "__main__":
    run()
