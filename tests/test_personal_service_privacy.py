"""Who sees what on a personal service.

The provider sees every request and booking. A requester sees only their own.
Anyone else, signed in or not, sees open and taken slots but never who asked,
what they asked for, or who was booked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import insert, select, update

from app.auth.jwt import create_access_token
from app.models import project_activities, project_service_requests, projects
from tests.conftest import seed_channel_with_membership, seed_user


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


SECRET_TITLE = "Zebracrossing tutoring"
SECRET_BODY = "Please help with my private paperwork"


def _setup(db_transaction, client):
    creator_id, creator_name = seed_user(db_transaction, username_prefix="psp-creator")
    requester_id, requester_name = seed_user(db_transaction, username_prefix="psp-req")
    stranger_id, _ = seed_user(db_transaction, username_prefix="psp-stranger")
    _channel_id, channel_slug = seed_channel_with_membership(db_transaction, creator_id=creator_id)
    db_transaction.flush()

    created = client.post(
        "/projects",
        headers=_auth(creator_id),
        json={
            "title": "Tutoring",
            "description": "One-on-one help",
            "project_mode": "personal-service",
            "location_label": "Online",
            "channel_slugs": [channel_slug],
            "request_mode": "both",
        },
    )
    assert created.status_code == 200, created.text
    slug = created.json()["project"]["slug"]

    weekday = (datetime.now(UTC).weekday() + 1) % 7
    rule = client.post(
        f"/projects/{slug}/service-availability",
        headers=_auth(creator_id),
        json={
            "weekday": weekday,
            "start_time": time(10, 0).strftime("%H:%M:%S"),
            "end_time": time(12, 0).strftime("%H:%M:%S"),
            "timezone": "UTC",
            "note": "Office hours",
        },
    )
    assert rule.status_code == 200, rule.text

    slots = client.get(f"/projects/{slug}", headers=_auth(requester_id)).json()["lifecycle"][
        "personalService"
    ]["availabilitySlots"]
    slot = next(item for item in slots if not item.get("held"))

    booked = client.post(
        f"/projects/{slug}/service-requests",
        headers=_auth(requester_id),
        json={
            "title": SECRET_TITLE,
            "body": SECRET_BODY,
            "scheduled_at": slot["startAt"],
            "ends_at": slot["endAt"],
        },
    )
    assert booked.status_code == 200, booked.text
    accept = client.patch(
        f"/projects/{slug}/service-requests/{booked.json()['request']['id']}",
        headers=_auth(creator_id),
        json={"status": "accepted", "hold_slot": True},
    )
    assert accept.status_code == 200, accept.text

    pending = client.post(
        f"/projects/{slug}/service-requests",
        headers=_auth(requester_id),
        json={"title": f"{SECRET_TITLE} two", "body": SECRET_BODY},
    )
    assert pending.status_code == 200, pending.text

    return {
        "slug": slug,
        "slot_id": slot["id"],
        "creator": _auth(creator_id),
        "creator_name": creator_name,
        "requester": _auth(requester_id),
        "requester_name": requester_name,
        "stranger": _auth(stranger_id),
    }


def _public_payloads(client, slug: str, headers: dict[str, str]) -> list[str]:
    bodies = []
    for path in (
        f"/projects/{slug}",
        f"/projects/{slug}/history",
        f"/projects/{slug}/service-availability",
    ):
        response = client.get(path, headers=headers)
        if response.status_code == 200:
            bodies.append(json.dumps(response.json()))
    return bodies


def test_strangers_and_visitors_never_see_who_requested(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)

    for label, headers in (("stranger", ctx["stranger"]), ("visitor", {})):
        for body in _public_payloads(isolated_client, ctx["slug"], headers):
            assert ctx["requester_name"] not in body, f"{label} sees the requester"
            assert SECRET_TITLE not in body, f"{label} sees the request title"
            assert SECRET_BODY not in body, f"{label} sees the request body"

        listed = isolated_client.get(f"/projects/{ctx['slug']}/service-requests", headers=headers)
        if listed.status_code == 200:
            assert listed.json()["items"] == [], f"{label} can list requests"

        detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=headers).json()
        system = detail["lifecycle"]["requestSystem"]
        assert system["requests"] == []
        assert system["viewerCanReviewRequests"] is False
        assert system["viewerCanRequestSettingsChanges"] is False
        assert detail["lifecycle"]["phaseFive"]["viewerCanCreateActivities"] is False
        slot = next(
            item
            for item in detail["lifecycle"]["personalService"]["availabilitySlots"]
            if item["id"] == ctx["slot_id"]
        )
        assert slot["held"] is True
        assert slot["bookings"] == []
        assert not slot.get("viewerBooked")


def test_visitor_cannot_request(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)
    detail = isolated_client.get(f"/projects/{ctx['slug']}").json()
    assert detail["lifecycle"]["requestSystem"]["viewerCanSubmitRequests"] is False
    refused = isolated_client.post(
        f"/projects/{ctx['slug']}/service-requests",
        json={"title": "Anonymous", "body": "Hello"},
    )
    assert refused.status_code in {401, 403}


def test_requester_sees_only_their_own(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)
    other_id, _ = seed_user(db_transaction, username_prefix="psp-other")
    db_transaction.flush()
    other = _auth(other_id)
    isolated_client.post(
        f"/projects/{ctx['slug']}/service-requests",
        headers=other,
        json={"title": "Other person's request", "body": "Keep this between us"},
    )

    detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=ctx["requester"]).json()
    requests = detail["lifecycle"]["requestSystem"]["requests"]
    assert {item["title"] for item in requests} == {SECRET_TITLE, f"{SECRET_TITLE} two"}
    assert "Keep this between us" not in json.dumps(detail)
    assert detail["lifecycle"]["requestSystem"]["viewerCanReviewRequests"] is False
    slot = next(
        item
        for item in detail["lifecycle"]["personalService"]["availabilitySlots"]
        if item["id"] == ctx["slot_id"]
    )
    assert slot["held"] is True
    assert slot["viewerBooked"] is True
    assert slot["bookings"] == []


def test_provider_sees_everything(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)
    detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=ctx["creator"]).json()
    system = detail["lifecycle"]["requestSystem"]
    assert system["viewerCanReviewRequests"] is True
    assert {item["requesterUsername"] for item in system["requests"]} == {ctx["requester_name"]}
    slot = next(
        item
        for item in detail["lifecycle"]["personalService"]["availabilitySlots"]
        if item["id"] == ctx["slot_id"]
    )
    assert slot["bookings"][0]["requesterUsername"] == ctx["requester_name"]


def test_finished_booking_stays_between_provider_and_requester(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)
    creator_detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=ctx["creator"]).json()
    accepted = next(
        item
        for item in creator_detail["lifecycle"]["requestSystem"]["requests"]
        if item["title"] == SECRET_TITLE
    )
    ended = datetime.now(UTC) - timedelta(hours=1)
    project_id = db_transaction.execute(
        select(projects.c.id, projects.c.author_id).where(projects.c.slug == ctx["slug"])
    ).one()
    activity_id = db_transaction.execute(
        insert(project_activities)
        .values(
            project_id=project_id[0],
            linked_request_id=accepted["id"],
            title=f"Session with {ctx['requester_name']}",
            author_id=project_id[1],
            scheduled_at=ended - timedelta(hours=1),
            ends_at=ended,
            location_label="Online",
            note="See you then",
            status="active",
        )
        .returning(project_activities.c.id)
    ).scalar_one()
    db_transaction.execute(
        update(project_service_requests)
        .where(project_service_requests.c.id == accepted["id"])
        .values(status="planned", linked_activity_id=activity_id)
    )
    db_transaction.flush()

    def history_ids(headers):
        detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=headers).json()
        return {item["id"] for item in detail["lifecycle"]["phaseFive"]["history"]}, detail

    creator_ids, _ = history_ids(ctx["creator"])
    requester_ids, requester_detail = history_ids(ctx["requester"])
    stranger_ids, stranger_detail = history_ids(ctx["stranger"])
    visitor_ids, visitor_detail = history_ids({})

    assert str(activity_id) in creator_ids
    assert str(activity_id) in requester_ids
    own = next(
        item
        for item in requester_detail["lifecycle"]["phaseFive"]["history"]
        if item["id"] == str(activity_id)
    )
    assert own["viewerCanRate"] is True
    assert str(activity_id) not in stranger_ids
    assert str(activity_id) not in visitor_ids
    for detail in (stranger_detail, visitor_detail):
        assert ctx["requester_name"] not in json.dumps(detail)
