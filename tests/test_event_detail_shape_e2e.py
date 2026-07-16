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

EVENT_PAGE_DATA_KEYS = {
    "id",
    "slug",
    "createdAt",
    "title",
    "description",
    "isPrivate",
    "scheduledAt",
    "channelTags",
    "communityTags",
    "createdByUsername",
    "timeLabel",
    "locationLabel",
    "voteCount",
    "activeVote",
    "commentCount",
    "memberCount",
    "lastActivityAt",
    "signalSummary",
    "lifecycle",
    "attendanceNote",
    "agenda",
    "updates",
    "updateRequests",
    "viewerCanRequestUpdate",
    "viewerCanVoteOnUpdateRequests",
    "editRequests",
    "viewerCanRequestEdit",
    "viewerCanVoteOnEditRequests",
    "history",
    "attendees",
    "invitedUsernames",
    "eventEditors",
    "members",
    "viewerIsMember",
    "viewerCanToggleMembership",
    "viewerHasEventEditAccess",
    "viewerCanManageEditors",
    "viewerCanShare",
    "availableEditorInvitees",
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

    username = f"eventdetail-{str(user_id)[:8]}"
    channel_slug = f"eventch-{str(channel_id)[:8]}"

    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash="x",
            bio="event detail test user",
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Event Detail Test Channel",
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
    event_slug = f"detail-event-{str(uuid4())[:8]}"

    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        create_resp = client.post(
            "/events",
            headers=headers,
            json={
                "slug": event_slug,
                "title": "Event Detail Shape",
                "description": "Verifies event detail shape.",
                "is_private": False,
                "time_label": "This weekend",
                "location_label": "online",
                "channel_slugs": [channel_slug],
            },
        )
        assert create_resp.status_code == 200, create_resp.text

        detail_resp = client.get(f"/events/{event_slug}", headers=headers)
        assert detail_resp.status_code == 200, detail_resp.text

        payload = detail_resp.json()
        assert "event" not in payload

        actual_keys = set(payload.keys())
        missing = sorted(EVENT_PAGE_DATA_KEYS - actual_keys)
        extras = sorted(actual_keys - EVENT_PAGE_DATA_KEYS)

        assert actual_keys == EVENT_PAGE_DATA_KEYS, (
            f"Top-level keys mismatch. Missing={missing}; Extra={extras}"
        )

        print(
            json.dumps(
                {
                    "event_slug": event_slug,
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
