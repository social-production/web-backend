"""Community member lists, shares landing in chat, and the provider's side of a booking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.models import project_activities, project_service_requests, projects
from tests.conftest import seed_user
from tests.test_personal_service_privacy import SECRET_TITLE, _auth, _setup


def test_channel_members_are_listed_with_standing_even_signed_out(
    db_transaction, isolated_client
):
    from tests.conftest import seed_channel_with_membership

    creator_id, creator_name = seed_user(db_transaction, username_prefix="mem-creator")
    _channel_id, slug = seed_channel_with_membership(db_transaction, creator_id=creator_id)
    db_transaction.flush()

    for headers in ({}, _auth(creator_id)):
        listed = isolated_client.get(f"/scopes/channels/{slug}/members", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        mine = next(item for item in items if item["username"] == creator_name)
        assert mine["standing"] in {"new", "warm", "full", "paused"}


def test_sharing_a_project_drops_the_link_into_the_direct_chat(db_transaction, isolated_client):
    ctx = _setup(db_transaction, isolated_client)
    shared = isolated_client.post(
        f"/projects/{ctx['slug']}/share",
        headers=ctx["creator"],
        json={"username": ctx["requester_name"]},
    )
    assert shared.status_code == 200, shared.text
    assert shared.json()["ok"] is True

    chats = isolated_client.get("/messages/conversations", headers=ctx["requester"]).json()
    direct = next(item for item in chats["items"] if item["kind"] == "direct")
    lines = isolated_client.get(
        f"/messages/conversations/{direct['id']}/messages", headers=ctx["requester"]
    ).json()["items"]
    assert any(f"Open project: /projects/{ctx['slug']}" in item["body"] for item in lines)


def test_provider_can_rate_a_finished_booking_and_sees_who_booked(
    db_transaction, isolated_client
):
    ctx = _setup(db_transaction, isolated_client)
    project_id = db_transaction.execute(
        select(projects.c.id).where(projects.c.slug == ctx["slug"])
    ).scalar_one()
    request_row = (
        db_transaction.execute(
            select(project_service_requests).where(
                project_service_requests.c.project_id == project_id,
                project_service_requests.c.title == SECRET_TITLE,
            )
        )
        .mappings()
        .one()
    )
    booking_id = request_row["linked_activity_id"]
    assert booking_id is not None
    ended = datetime.now(UTC) - timedelta(minutes=5)
    db_transaction.execute(
        update(project_activities)
        .where(project_activities.c.id == booking_id)
        .values(scheduled_at=ended - timedelta(hours=1), ends_at=ended)
    )
    db_transaction.flush()

    detail = isolated_client.get(f"/projects/{ctx['slug']}", headers=ctx["creator"]).json()
    item = next(
        entry
        for entry in detail["lifecycle"]["phaseFive"]["history"]
        if entry["id"] == str(booking_id)
    )
    assert item["viewerCanRate"] is True
    assert item["requesterUsername"] == ctx["requester_name"]
    assert item["aggregateCompletionLabel"] != "Marked uncompleted"

    rated = isolated_client.put(
        f"/projects/{ctx['slug']}/activities/{booking_id}/rating",
        headers=ctx["creator"],
        json={"rating": 5, "comment": "Great session"},
    )
    assert rated.status_code == 200, rated.text
