from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import channels, users


def _request_json(
    url: str, method: str = "GET", body: dict[str, object] | None = None, token: str | None = None
) -> dict[str, object]:
    payload = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_json_or_closed(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    token: str | None = None,
    expected_closed_detail: str | None = None,
) -> dict[str, object] | None:
    try:
        return _request_json(url, method=method, body=body, token=token)
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
        payload = json.loads(exc.read().decode("utf-8"))
        if expected_closed_detail is not None:
            assert payload.get("detail") == expected_closed_detail
        return None


def _seed_users_and_channel() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    user_a = uuid4()
    user_b = uuid4()
    channel_id = uuid4()
    channel_slug = f"evt-ch-{str(channel_id)[:8]}"

    db.execute(
        insert(users).values(
            id=user_a,
            username=f"evta-{str(user_a)[:8]}",
            email=f"evta-{str(user_a)[:8]}@t.invalid",
            password_hash="x",
            bio="a",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=user_b,
            username=f"evtb-{str(user_b)[:8]}",
            email=f"evtb-{str(user_b)[:8]}@t.invalid",
            password_hash="x",
            bio="b",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Event Test Channel",
            description="seed",
            created_by=user_a,
            created_at=now,
            updated_at=now,
        )
    )

    db.commit()
    db.close()

    return {
        "token_a": create_access_token(str(user_a)),
        "token_b": create_access_token(str(user_b)),
        "channel_slug": channel_slug,
    }


def run() -> None:
    base = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8010")
    seeded = _seed_users_and_channel()

    event_slug = f"evt-{str(uuid4())[:8]}"
    created = _request_json(
        f"{base}/events",
        method="POST",
        token=seeded["token_a"],
        body={
            "slug": event_slug,
            "title": "Event Test",
            "description": "Event description",
            "is_private": False,
            "time_label": "Soon",
            "location_label": "Online",
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert created["event"]["slug"] == event_slug

    joined = _request_json(
        f"{base}/events/{event_slug}/join",
        method="POST",
        token=seeded["token_b"],
    )
    assert joined["ok"] is True

    plan = _request_json(
        f"{base}/events/{event_slug}/plans",
        method="POST",
        token=seeded["token_a"],
        body={
            "title": "Plan A",
            "description": "Plan details",
            "demand_consideration_note": "note",
            "location_label": "Online",
            "schedule_payload": {"window": "today"},
            "plan_payload": {"steps": 3},
        },
    )
    plan_id = plan["plan"]["id"]

    first_plan_vote = _request_json(
        f"{base}/events/{event_slug}/plans/{plan_id}/vote",
        method="POST",
        token=seeded["token_a"],
        body={"vote": "yes"},
    )
    voted_plan = (
        _request_json_or_closed(
            f"{base}/events/{event_slug}/plans/{plan_id}/vote",
            method="POST",
            token=seeded["token_b"],
            body={"vote": "yes"},
        )
        or first_plan_vote
    )
    assert voted_plan["is_leading"] is True
    assert voted_plan["plan"]["vote_summary"]["is_winning"] is True

    phase_req = _request_json(
        f"{base}/events/{event_slug}/phase-requests",
        method="POST",
        token=seeded["token_a"],
        body={"target_phase_id": "phase-2", "reason": "Ready"},
    )
    phase_request_id = phase_req["request"]["id"]

    first_phase_vote = _request_json(
        f"{base}/events/{event_slug}/phase-requests/{phase_request_id}/vote",
        method="POST",
        token=seeded["token_a"],
        body={"vote": "yes"},
    )
    phase_vote = (
        _request_json_or_closed(
            f"{base}/events/{event_slug}/phase-requests/{phase_request_id}/vote",
            method="POST",
            token=seeded["token_b"],
            body={"vote": "yes"},
            expected_closed_detail="Phase change request is already closed",
        )
        or first_phase_vote
    )
    assert phase_vote["executed"] is True
    assert phase_vote["current_phase_id"] == "phase-2"

    update_req = _request_json(
        f"{base}/events/{event_slug}/update-requests",
        method="POST",
        token=seeded["token_a"],
        body={"body": "Update body"},
    )
    update_request_id = update_req["request"]["id"]

    first_update_vote = _request_json(
        f"{base}/events/{event_slug}/update-requests/{update_request_id}/vote",
        method="POST",
        token=seeded["token_a"],
        body={"vote": "yes"},
    )
    update_vote = (
        _request_json_or_closed(
            f"{base}/events/{event_slug}/update-requests/{update_request_id}/vote",
            method="POST",
            token=seeded["token_b"],
            body={"vote": "yes"},
            expected_closed_detail="Update request is already closed",
        )
        or first_update_vote
    )
    assert update_vote["executed"] is True
    assert update_vote["request"]["status"] == "approved"

    edit_req = _request_json(
        f"{base}/events/{event_slug}/edit-requests",
        method="POST",
        token=seeded["token_a"],
        body={"title": "Edited Event Title", "description": "Edited Event Description"},
    )
    edit_request_id = edit_req["request"]["id"]

    first_edit_vote = _request_json(
        f"{base}/events/{event_slug}/edit-requests/{edit_request_id}/vote",
        method="POST",
        token=seeded["token_a"],
        body={"vote": "yes"},
    )
    edit_vote = (
        _request_json_or_closed(
            f"{base}/events/{event_slug}/edit-requests/{edit_request_id}/vote",
            method="POST",
            token=seeded["token_b"],
            body={"vote": "yes"},
            expected_closed_detail="Edit request is already closed",
        )
        or first_edit_vote
    )
    assert edit_vote["executed"] is True
    assert edit_vote["request"]["status"] == "approved"

    refreshed_event = _request_json(f"{base}/events/{event_slug}")
    assert refreshed_event["title"] == "Edited Event Title"
    assert refreshed_event["description"] == "Edited Event Description"

    phase_list = _request_json(f"{base}/events/{event_slug}/phase-requests")
    update_list = _request_json(f"{base}/events/{event_slug}/update-requests")
    edit_list = _request_json(f"{base}/events/{event_slug}/edit-requests")
    plans_list = _request_json(f"{base}/events/{event_slug}/plans")

    assert phase_list["total"] >= 1
    assert update_list["total"] >= 1
    assert edit_list["total"] >= 1
    assert plans_list["total"] >= 1

    print(
        json.dumps(
            {
                "event_slug": event_slug,
                "plan_winning": voted_plan["plan"]["vote_summary"]["is_winning"],
                "phase_executed": phase_vote["executed"],
                "update_executed": update_vote["executed"],
                "edit_executed": edit_vote["executed"],
                "current_phase_id": phase_vote["current_phase_id"],
                "final_title": refreshed_event["title"],
            }
        )
    )


if __name__ == "__main__":
    run()
