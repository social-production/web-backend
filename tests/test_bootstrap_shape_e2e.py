from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import users

BOOTSTRAP_PAYLOAD_KEYS = {
    "viewer",
    "featureFlags",
    "unreadCounts",
    "directory",
    "suggestedContacts",
    "activityRail",
    "activityRailHistory",
}


def _seed_user_token() -> str:
    db = SessionLocal()
    now = datetime.now(UTC)

    user_id = uuid4()
    username = f"bootstrapshape-{str(user_id)[:8]}"

    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash="x",
            bio="bootstrap shape test user",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.close()

    return create_access_token(str(user_id))


def run() -> None:
    token = _seed_user_token()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        resp = client.get("/bootstrap", headers=headers)
        assert resp.status_code == 200, resp.text

        payload = resp.json()
        actual_keys = set(payload.keys())
        missing = sorted(BOOTSTRAP_PAYLOAD_KEYS - actual_keys)
        extra = sorted(actual_keys - BOOTSTRAP_PAYLOAD_KEYS)

        assert actual_keys == BOOTSTRAP_PAYLOAD_KEYS, (
            f"Top-level keys mismatch. Missing={missing}; Extra={extra}"
        )

        print(
            json.dumps(
                {
                    "status": resp.status_code,
                    "key_count": len(actual_keys),
                    "missing": missing,
                    "extra": extra,
                    "activityRailType": type(payload["activityRail"]).__name__,
                }
            )
        )


def test_bootstrap_summary_returns_nested_unread_counts() -> None:
    token = _seed_user_token()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        response = client.get("/bootstrap/summary", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"unreadCounts"}
    assert set(payload["unreadCounts"].keys()) == {"notifications", "messages"}
    assert isinstance(payload["unreadCounts"]["notifications"], int)
    assert isinstance(payload["unreadCounts"]["messages"], int)


def test_bootstrap_summary_guest_returns_zero_counts() -> None:
    with TestClient(app) as client:
        response = client.get("/bootstrap/summary")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "unreadCounts": {
            "notifications": 0,
            "messages": 0,
        },
    }


if __name__ == "__main__":
    run()
