from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from app.auth.jwt import create_access_token
from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import channels, events, locations, users
from app.services.locations import serialize_location
from tests.conftest import private_event_plan_fields


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _create_user(username: str) -> str:
    db = SessionLocal()
    user_id = uuid4()
    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@t.invalid",
            password_hash=hash_password("password-123"),
            bio=username,
            is_active=True,
        )
    )
    db.commit()
    db.close()
    return str(user_id)


def _auth_headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def test_create_location_and_attach_to_public_event(client: TestClient):
    owner = _unique("loc-owner")
    owner_id = _create_user(owner)
    headers = _auth_headers(owner_id)

    db = SessionLocal()
    channel_id = uuid4()
    channel_slug = _unique("loc-ch")
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name=channel_slug,
            description="seed",
            created_by=owner_id,
        )
    )
    db.commit()
    db.close()

    created = client.post(
        "/locations",
        headers=headers,
        json={
            "display_label": "Exact Venue Hall",
            "latitude": 51.507351,
            "longitude": -0.127758,
            "region": "London",
            "country": "United Kingdom",
            "precision": "exact",
            "provider_place_id": "nominatim:123",
        },
    )
    assert created.status_code == 200, created.text
    location = created.json()["location"]
    assert location["precision"] == "exact"
    assert location["latitude"] == 51.507351

    approx = client.post(
        "/locations",
        headers=headers,
        json={
            "display_label": "Approx Area",
            "latitude": 51.507351,
            "longitude": -0.127758,
            "precision": "approximate",
        },
    )
    assert approx.status_code == 200
    approx_loc = approx.json()["location"]
    assert approx_loc["precision"] == "approximate"
    assert approx_loc["latitude"] == 51.51
    assert approx_loc["longitude"] == -0.13

    slug = _unique("loc-evt")
    response = client.post(
        "/events",
        headers=headers,
        json={
            "slug": slug,
            "title": "Located Event",
            "description": "desc",
            "audience": "public",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
            "location_label": "Exact Venue Hall",
            "time_label": "TBD",
        },
    )
    assert response.status_code == 200, response.text
    event_slug = response.json()["event"]["slug"]

    db = SessionLocal()
    db.execute(update(events).where(events.c.slug == event_slug).values(location_id=location["id"]))
    db.commit()
    db.close()

    detail = client.get(f"/events/{event_slug}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["location"]["id"] == location["id"]
    assert body["location"]["precision"] == "exact"
    assert body["mapEligible"] is True


def test_private_event_location_redacted_from_serialize_helper():
    row = {
        "id": uuid4(),
        "provider_place_id": None,
        "display_label": "Invite-only loft",
        "latitude": 40.7128,
        "longitude": -74.006,
        "region": "NY",
        "country": "US",
        "precision": "exact",
        "is_online": False,
    }
    assert serialize_location(row, viewer_authorized=False, is_private_entity=True) is None
    visible = serialize_location(row, viewer_authorized=True, is_private_entity=True)
    assert visible is not None
    assert visible["display_label"] == "Invite-only loft"


def test_private_event_hides_location_from_strangers(client: TestClient):
    owner = _unique("priv-loc-owner")
    stranger = _unique("priv-loc-stranger")
    owner_id = _create_user(owner)
    stranger_id = _create_user(stranger)
    owner_headers = _auth_headers(owner_id)

    db = SessionLocal()
    channel_id = uuid4()
    channel_slug = _unique("priv-loc-ch")
    location_id = uuid4()
    now = datetime.now(UTC)
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name=channel_slug,
            description="seed",
            created_by=owner_id,
        )
    )
    db.execute(
        insert(locations).values(
            id=location_id,
            provider_place_id=None,
            display_label="Private loft",
            latitude=40.7128,
            longitude=-74.006,
            region="NY",
            country="US",
            precision="exact",
            is_online=False,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.close()

    slug = _unique("priv-loc-evt")
    created = client.post(
        "/events",
        headers=owner_headers,
        json={
            "slug": slug,
            "title": "Private Located",
            "description": "desc",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
            "location_label": "Private loft",
            "time_label": "TBD",
            **private_event_plan_fields(title="Private loft plan"),
        },
    )
    assert created.status_code == 200, created.text
    event_slug = created.json()["event"]["slug"]

    db = SessionLocal()
    db.execute(update(events).where(events.c.slug == event_slug).values(location_id=location_id))
    db.commit()
    db.close()

    member_detail = client.get(f"/events/{event_slug}", headers=owner_headers)
    assert member_detail.status_code == 200
    assert member_detail.json()["location"]["display_label"] == "Private loft"

    stranger_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(stranger_id))
    assert stranger_detail.status_code == 404
