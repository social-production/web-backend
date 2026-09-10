from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from app.auth.jwt import create_access_token
from tests.conftest import seed_channel_with_membership, seed_user


def _auth_header(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def test_personal_service_weekly_availability_and_slot_hold(db_transaction, isolated_client):
    creator_id, _ = seed_user(db_transaction, username_prefix="avail-creator")
    requester_id, _ = seed_user(db_transaction, username_prefix="avail-req")
    _channel_id, channel_slug = seed_channel_with_membership(db_transaction, creator_id=creator_id)
    db_transaction.flush()

    creator = _auth_header(creator_id)
    requester = _auth_header(requester_id)

    created = isolated_client.post(
        "/projects",
        headers=creator,
        json={
            "title": "Tutoring",
            "description": "One-on-one help",
            "project_mode": "personal-service",
            "location_label": "Online",
            "channel_slugs": [channel_slug],
            "request_mode": "calendar",
        },
    )
    assert created.status_code == 200, created.text
    slug = created.json()["project"]["slug"]

    weekday = datetime.now(UTC).weekday()
    start = (datetime.now(UTC) + timedelta(hours=2)).time().replace(second=0, microsecond=0)
    end = (datetime.now(UTC) + timedelta(hours=4)).time().replace(second=0, microsecond=0)
    if end <= start:
        start = time(10, 0)
        end = time(12, 0)
        weekday = (datetime.now(UTC).weekday() + 1) % 7

    rule = isolated_client.post(
        f"/projects/{slug}/service-availability",
        headers=creator,
        json={
            "weekday": weekday,
            "start_time": start.strftime("%H:%M:%S"),
            "end_time": end.strftime("%H:%M:%S"),
            "timezone": "UTC",
            "note": "Office hours",
        },
    )
    assert rule.status_code == 200, rule.text

    detail = isolated_client.get(f"/projects/{slug}", headers=requester)
    assert detail.status_code == 200, detail.text
    slots = detail.json()["lifecycle"]["personalService"]["availabilitySlots"]
    open_slots = [slot for slot in slots if not slot.get("held")]
    assert open_slots, slots
    slot = open_slots[0]

    request_resp = isolated_client.post(
        f"/projects/{slug}/service-requests",
        headers=requester,
        json={
            "title": "Need algebra help",
            "body": "Can we use this slot?",
            "scheduled_at": slot["startAt"],
            "ends_at": slot["endAt"],
        },
    )
    assert request_resp.status_code == 200, request_resp.text
    request_id = request_resp.json()["request"]["id"]
    assert request_resp.json().get("conversation_id")

    accept = isolated_client.patch(
        f"/projects/{slug}/service-requests/{request_id}",
        headers=creator,
        json={"status": "accepted", "hold_slot": True},
    )
    assert accept.status_code == 200, accept.text

    after = isolated_client.get(f"/projects/{slug}", headers=requester)
    requester_slots = after.json()["lifecycle"]["personalService"]["availabilitySlots"]
    assert all(item["id"] != slot["id"] or item.get("held") for item in requester_slots) or all(
        item["id"] != slot["id"] for item in requester_slots
    )

    creator_detail = isolated_client.get(f"/projects/{slug}", headers=creator)
    creator_slots = creator_detail.json()["lifecycle"]["personalService"]["availabilitySlots"]
    matching = [item for item in creator_slots if item["id"] == slot["id"]]
    assert matching
    assert matching[0].get("held") is True
    assert matching[0]["bookings"]


def test_project_role_suggestion_notifies_user(db_transaction, isolated_client):
    creator_id, _ = seed_user(db_transaction, username_prefix="role-creator")
    member_id, _ = seed_user(db_transaction, username_prefix="role-member")
    suggested_id, suggested_name = seed_user(db_transaction, username_prefix="role-suggested")
    _channel_id, channel_slug = seed_channel_with_membership(db_transaction, creator_id=creator_id)
    db_transaction.flush()

    creator = _auth_header(creator_id)
    member = _auth_header(member_id)
    suggested = _auth_header(suggested_id)

    created = isolated_client.post(
        "/projects",
        headers=creator,
        json={
            "title": "Garden bed",
            "description": "Build a raised bed",
            "project_mode": "productive",
            "location_label": "Online",
            "channel_slugs": [channel_slug],
        },
    )
    assert created.status_code == 200, created.text
    slug = created.json()["project"]["slug"]

    isolated_client.post(f"/projects/{slug}/join", headers=member)
    isolated_client.post(f"/projects/{slug}/join", headers=suggested)

    start = datetime.now(UTC) + timedelta(days=2)
    activity = isolated_client.post(
        f"/projects/{slug}/activities",
        headers=creator,
        json={
            "title": "Build day",
            "scheduled_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=3)).isoformat(),
            "location_label": "Workshop",
            "note": "Bring tools",
            "role_requirements": [
                {
                    "label": "Lead carpenter",
                    "required_count": 1,
                    "suggested_user_id": str(suggested_id),
                }
            ],
        },
    )
    assert activity.status_code == 200, activity.text
    activity_id = activity.json()["activity"]["id"]

    detail = isolated_client.get(f"/projects/{slug}", headers=member)
    roles = detail.json()["lifecycle"]["phaseFive"]["activities"][0]["roles"]
    assert roles[0]["suggestedUser"]["username"] == suggested_name
    role_id = roles[0]["id"]

    notify = isolated_client.get("/notifications", headers=suggested)
    assert notify.status_code == 200, notify.text
    kinds = [item["kind"] for item in notify.json()["items"]]
    assert "prj-role-suggest" in kinds

    decline = isolated_client.post(
        f"/projects/{slug}/activities/{activity_id}/roles/{role_id}/suggestion/decline",
        headers=suggested,
    )
    assert decline.status_code == 200, decline.text

    after = isolated_client.get(f"/projects/{slug}", headers=creator)
    after_roles = after.json()["lifecycle"]["phaseFive"]["activities"][0]["roles"]
    assert after_roles[0]["suggestedUser"] is None
