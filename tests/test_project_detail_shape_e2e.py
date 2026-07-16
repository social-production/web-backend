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

PROJECT_PAGE_DATA_KEYS = {
    "id",
    "slug",
    "createdAt",
    "title",
    "authorUsername",
    "projectMode",
    "projectSubtype",
    "description",
    "channelTags",
    "communityTags",
    "stage",
    "locationLabel",
    "voteCount",
    "activeVote",
    "signalCount",
    "commentCount",
    "memberCount",
    "lastActivityAt",
    "lifecycle",
    "updates",
    "updateRequests",
    "viewerCanRequestUpdate",
    "viewerCanVoteOnUpdateRequests",
    "editRequests",
    "viewerCanRequestEdit",
    "viewerCanVoteOnEditRequests",
    "linksFrame",
    "inventoryFrame",
    "history",
    "members",
    "viewerIsMember",
    "viewerCanToggleMembership",
    "viewerCanShare",
    "shareContacts",
    "report",
    "isRemovedByReport",
    "discussionNote",
    "discussion",
}


def _seed_user_and_channel() -> tuple[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    user_id = uuid4()
    channel_id = uuid4()

    username = f"detailuser-{str(user_id)[:8]}"
    channel_slug = f"detailch-{str(channel_id)[:8]}"

    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash="x",
            bio="detail test user",
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Detail Test Channel",
            description="seed",
            created_by=user_id,
            created_at=now,
            updated_at=now,
        )
    )

    db.commit()
    db.close()

    return create_access_token(str(user_id)), channel_slug


def run() -> None:
    token, channel_slug = _seed_user_and_channel()
    project_slug = f"detail-proj-{str(uuid4())[:8]}"

    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        create_resp = client.post(
            "/projects",
            headers=headers,
            json={
                "slug": project_slug,
                "title": "Detail Shape Project",
                "description": "Verifies project detail shape.",
                "project_mode": "productive",
                "project_subtype": "standard",
                "location_label": "online",
                "channel_slugs": [channel_slug],
            },
        )
        assert create_resp.status_code == 200, create_resp.text

        detail_resp = client.get(f"/projects/{project_slug}", headers=headers)
        assert detail_resp.status_code == 200, detail_resp.text

        payload = detail_resp.json()
        assert "project" not in payload

        actual_keys = set(payload.keys())
        missing = sorted(PROJECT_PAGE_DATA_KEYS - actual_keys)
        extras = sorted(actual_keys - PROJECT_PAGE_DATA_KEYS)

        assert actual_keys == PROJECT_PAGE_DATA_KEYS, (
            f"Top-level keys mismatch. Missing={missing}; Extra={extras}"
        )

        print(
            json.dumps(
                {
                    "project_slug": project_slug,
                    "create_status": create_resp.status_code,
                    "detail_status": detail_resp.status_code,
                    "key_count": len(actual_keys),
                    "missing": missing,
                    "extra": extras,
                }
            )
        )


if __name__ == "__main__":
    run()
