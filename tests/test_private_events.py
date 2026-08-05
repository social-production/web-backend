from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import channels, communities, scope_memberships, user_follows, users
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


def test_public_event_forced_collaborative(client: TestClient):
    owner = _unique("evt-owner")
    owner_id = _create_user(owner)
    headers = _auth_headers(owner_id)

    db = SessionLocal()
    channel_id = uuid4()
    channel_slug = _unique("evt-ch")
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

    response = client.post(
        "/events",
        headers=headers,
        json={
            "slug": _unique("pub-evt"),
            "title": "Public Collab",
            "description": "desc",
            "audience": "public",
            "governance": "organizer_controlled",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
        },
    )
    assert response.status_code == 422, response.text


def test_invite_only_adds_members_and_hides_from_public_feed(client: TestClient):
    owner = _unique("inv-owner")
    invitee = _unique("inv-user")
    owner_id = _create_user(owner)
    invitee_id = _create_user(invitee)
    headers = _auth_headers(owner_id)
    slug = _unique("inv-evt")

    created = client.post(
        "/events",
        headers=headers,
        json={
            "slug": slug,
            "title": "Invite Only",
            "description": "private invite event",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [invitee],
            **private_event_plan_fields(title="Invite Only Plan"),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()["event"]
    assert body["audience"] == "invite_only"
    assert body["governance"] == "organizer_controlled"
    assert body["is_private"] is True
    assert body["current_phase_id"] == "activity"
    event_slug = body["slug"]

    # Invitee can load detail
    invitee_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(invitee_id))
    assert invitee_detail.status_code == 200
    detail = invitee_detail.json()
    assert detail["lifecycle"]["currentPhaseId"] == "activity"
    assert detail["lifecycle"]["phaseOne"]["viewerCanSignalDemand"] is False
    assert detail["lifecycle"]["activity"]["viewerCanCreateActivities"] is False

    # Creator can create activities
    owner_detail = client.get(f"/events/{event_slug}", headers=headers)
    assert owner_detail.status_code == 200
    assert owner_detail.json()["lifecycle"]["activity"]["viewerCanCreateActivities"] is True

    # Stranger cannot
    stranger_id = _create_user(_unique("stranger"))
    stranger = client.get(f"/events/{event_slug}", headers=_auth_headers(stranger_id))
    assert stranger.status_code == 404

    public_feed = client.get("/feeds/public?filter=events")
    assert public_feed.status_code == 200
    assert all(item.get("slug") != event_slug for item in public_feed.json()["items"])


def test_private_community_event_discoverable_to_members(client: TestClient):
    owner = _unique("pc-owner")
    member = _unique("pc-member")
    outsider = _unique("pc-out")
    owner_id = _create_user(owner)
    member_id = _create_user(member)
    outsider_id = _create_user(outsider)

    db = SessionLocal()
    community_id = uuid4()
    community_slug = _unique("pc-comm")
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=community_slug,
            name=community_slug,
            description="private community",
            join_policy="closed",
            created_by=owner_id,
        )
    )
    for uid in (owner_id, member_id):
        db.execute(
            insert(scope_memberships).values(
                scope_kind="community",
                scope_id=community_id,
                user_id=uid,
                role="member",
            )
        )
    db.commit()
    db.close()

    slug = _unique("pc-evt")
    created = client.post(
        "/events",
        headers=_auth_headers(owner_id),
        json={
            "slug": slug,
            "title": "Community Private",
            "description": "scoped",
            "audience": "private_community",
            "governance": "collaborative",
            "home_community_slug": community_slug,
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [community_slug],
            "invited_usernames": [],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["event"]["audience"] == "private_community"
    assert created.json()["event"]["current_phase_id"] == "proposal"
    event_slug = created.json()["event"]["slug"]

    member_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(member_id))
    assert member_detail.status_code == 200
    assert member_detail.json()["viewerIsMember"] is False
    assert member_detail.json()["viewerCanToggleMembership"] is True
    assert member_detail.json()["lifecycle"]["phaseOne"]["viewerCanSignalDemand"] is True

    outsider_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(outsider_id))
    assert outsider_detail.status_code == 404


def test_private_collaborative_invite_only_starts_at_proposal(client: TestClient):
    owner = _unique("collab-owner")
    invitee = _unique("collab-invitee")
    owner_id = _create_user(owner)
    invitee_id = _create_user(invitee)
    headers = _auth_headers(owner_id)

    created = client.post(
        "/events",
        headers=headers,
        json={
            "title": "Private Collaborative",
            "description": "full lifecycle inside invite-only",
            "audience": "invite_only",
            "governance": "collaborative",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [invitee],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()["event"]
    assert body["current_phase_id"] == "proposal"
    assert body["governance"] == "collaborative"

    invitee_detail = client.get(f"/events/{body['slug']}", headers=_auth_headers(invitee_id))
    assert invitee_detail.status_code == 200
    detail = invitee_detail.json()
    assert detail["lifecycle"]["currentPhaseId"] == "proposal"
    assert detail["lifecycle"]["phaseOne"]["viewerCanSignalDemand"] is True
    assert detail["lifecycle"]["phaseOne"]["viewerCanAddValue"] is True


def test_private_event_requires_plan_payload(client: TestClient):
    owner = _unique("plan-req")
    owner_id = _create_user(owner)
    response = client.post(
        "/events",
        headers=_auth_headers(owner_id),
        json={
            "title": "Missing Plan",
            "description": "should fail",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [],
        },
    )
    assert response.status_code == 422, response.text


def test_private_collaborative_does_not_require_plan_payload(client: TestClient):
    owner = _unique("collab-plan")
    owner_id = _create_user(owner)
    response = client.post(
        "/events",
        headers=_auth_headers(owner_id),
        json={
            "title": "No Plan Needed",
            "description": "collaborative private",
            "audience": "invite_only",
            "governance": "collaborative",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["event"]["current_phase_id"] == "proposal"


def test_private_event_organizer_can_invite_and_promote(client: TestClient):
    owner = _unique("org-owner")
    member = _unique("org-member")
    outsider = _unique("org-out")
    promotee = _unique("org-promote")
    owner_id = _create_user(owner)
    member_id = _create_user(member)
    _create_user(outsider)
    promotee_id = _create_user(promotee)
    headers = _auth_headers(owner_id)
    slug = _unique("org-evt")

    created = client.post(
        "/events",
        headers=headers,
        json={
            "slug": slug,
            "title": "Organizer Authority",
            "description": "private organizer event",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [member],
            **private_event_plan_fields(title="Authority Plan"),
        },
    )
    assert created.status_code == 200, created.text
    event_slug = created.json()["event"]["slug"]

    detail = client.get(f"/events/{event_slug}", headers=headers)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["viewerCanManageEditors"] is True
    assert payload["viewerCanShare"] is True
    assert any(editor["username"] == owner for editor in payload["eventEditors"])
    assert all(member_row["username"] != owner for member_row in payload["members"])

    member_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(member_id))
    assert member_detail.status_code == 200
    assert member_detail.json()["viewerCanManageEditors"] is False
    assert member_detail.json()["viewerCanShare"] is False

    denied_share = client.post(
        f"/events/{event_slug}/share",
        headers=_auth_headers(member_id),
        json={"username": outsider},
    )
    assert denied_share.status_code == 403

    invited = client.post(
        f"/events/{event_slug}/share",
        headers=headers,
        json={"username": promotee},
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["ok"] is True

    granted = client.post(
        f"/events/{event_slug}/editors/grant",
        headers=headers,
        json={"user_id": promotee_id},
    )
    assert granted.status_code == 200, granted.text

    promotee_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(promotee_id))
    assert promotee_detail.status_code == 200
    promotee_payload = promotee_detail.json()
    assert promotee_payload["viewerIsOrganizer"] is True
    assert promotee_payload["viewerCanManageEditors"] is True
    assert promotee_payload["viewerCanShare"] is True

    outsider_share = client.post(
        f"/events/{event_slug}/share",
        headers=_auth_headers(promotee_id),
        json={"username": outsider},
    )
    assert outsider_share.status_code == 200, outsider_share.text
    assert outsider_share.json()["ok"] is True


def test_create_time_organizers_effective_for_collaborative_private(client: TestClient):
    owner = _unique("collab-owner")
    co_organizer = _unique("collab-org")
    invitee = _unique("collab-mem")
    outsider = _unique("collab-out")
    owner_id = _create_user(owner)
    co_organizer_id = _create_user(co_organizer)
    _create_user(invitee)
    _create_user(outsider)

    created = client.post(
        "/events",
        headers=_auth_headers(owner_id),
        json={
            "slug": _unique("collab-evt"),
            "title": "Collaborative Private",
            "description": "create-time organizers should invite immediately",
            "audience": "invite_only",
            "governance": "collaborative",
            "time_label": "TBD",
            "location_label": "TBD",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [invitee],
            "editor_usernames": [co_organizer],
        },
    )
    assert created.status_code == 200, created.text
    event_slug = created.json()["event"]["slug"]
    assert created.json()["event"]["current_phase_id"] == "proposal"

    co_detail = client.get(f"/events/{event_slug}", headers=_auth_headers(co_organizer_id))
    assert co_detail.status_code == 200, co_detail.text
    payload = co_detail.json()
    assert payload["viewerIsOrganizer"] is True
    assert payload["viewerCanShare"] is True
    assert any(editor["username"] == co_organizer for editor in payload["eventEditors"])

    invited = client.post(
        f"/events/{event_slug}/share",
        headers=_auth_headers(co_organizer_id),
        json={"username": outsider},
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["ok"] is True


def test_people_suggestions_rank_following_then_followers(client: TestClient):
    prefix = f"rank-{uuid4().hex[:8]}-"
    viewer = _unique(f"{prefix}viewer")
    followed = _unique(f"{prefix}followed")
    follower = _unique(f"{prefix}follower")
    stranger = _unique(f"{prefix}stranger")
    viewer_id = _create_user(viewer)
    followed_id = _create_user(followed)
    follower_id = _create_user(follower)
    _create_user(stranger)

    db = SessionLocal()
    db.execute(
        insert(user_follows).values(
            follower_id=viewer_id,
            followed_id=followed_id,
            status="accepted",
        )
    )
    db.execute(
        insert(user_follows).values(
            follower_id=follower_id,
            followed_id=viewer_id,
            status="accepted",
        )
    )
    db.commit()
    db.close()

    response = client.get(
        f"/users/suggestions?q={prefix}&limit=40",
        headers=_auth_headers(viewer_id),
    )
    assert response.status_code == 200, response.text
    usernames = [item["username"] for item in response.json()["items"]]
    assert followed in usernames
    assert follower in usernames
    assert stranger in usernames
    assert usernames.index(followed) < usernames.index(follower)
    assert usernames.index(follower) < usernames.index(stranger)

    contacts = client.get(
        f"/messages/contacts?q={prefix}&limit=25",
        headers=_auth_headers(viewer_id),
    )
    assert contacts.status_code == 200, contacts.text
    contact_names = [item["username"] for item in contacts.json()["items"]]
    assert contact_names.index(followed) < contact_names.index(follower)
    assert contact_names.index(follower) < contact_names.index(stranger)
