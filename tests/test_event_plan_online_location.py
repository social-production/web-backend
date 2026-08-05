from __future__ import annotations

from sqlalchemy import select

from app.auth.jwt import create_access_token
from app.models import event_plans, events, locations
from tests.conftest import private_event_plan_fields, seed_channel_with_membership, seed_user


def test_online_event_plan_persists_location_and_syncs_event(db_session, client) -> None:
    user_id, _username = seed_user(db_session, username_prefix="online-plan")
    _channel_id, channel_slug = seed_channel_with_membership(db_session, creator_id=user_id)
    db_session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(str(user_id))}"}

    created = client.post(
        "/events",
        headers=headers,
        json={
            "title": "Online Event",
            "description": "Remote meetup",
            "governance": "organizer_controlled",
            "audience": "invite_only",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
            "time_label": "TBD",
            "location_label": "TBD",
            **private_event_plan_fields(title="Online seed plan"),
        },
    )
    assert created.status_code == 200, created.text
    event_slug = created.json()["event"]["slug"]

    response = client.post(
        f"/events/{event_slug}/plans",
        headers=headers,
        json={
            "title": "Online plan",
            "description": "Meet remotely",
            "demand_consideration_note": "",
            "location_label": "Online",
            "is_online": True,
            "schedule_payload": {},
            "plan_payload": {"planPhases": [{"title": "Meetup", "details": "Join the call"}]},
        },
    )
    assert response.status_code == 200, response.text

    event_row = (
        db_session.execute(
            select(events.c.id, events.c.location_id, events.c.location_label).where(
                events.c.slug == event_slug
            )
        )
        .mappings()
        .one()
    )
    assert event_row["location_label"] == "Online"
    assert event_row["location_id"] is not None

    location_row = (
        db_session.execute(
            select(locations.c.is_online).where(locations.c.id == event_row["location_id"])
        )
        .mappings()
        .one()
    )
    assert location_row["is_online"] is True

    plan_row = (
        db_session.execute(
            select(event_plans.c.location_id).where(
                event_plans.c.event_id == event_row["id"],
                event_plans.c.is_leading.is_(True),
            )
        )
        .mappings()
        .one()
    )
    assert plan_row["location_id"] == event_row["location_id"]
