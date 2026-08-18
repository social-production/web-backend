"""Region feed and map marker privacy/distance tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, update

from app.auth.jwt import create_access_token
from app.models import events, project_service_request_settings
from app.services.feeds.region import haversine_km
from tests.conftest import (
    future_scheduled_at,
    private_event_plan_fields,
    seed_channel_with_membership,
    seed_user,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_location(client, headers: dict[str, str], *, label: str, lat: float, lon: float) -> str:
    response = client.post(
        "/locations",
        headers=headers,
        json={
            "display_label": label,
            "latitude": lat,
            "longitude": lon,
            "region": "Victoria" if lat < -35 else "New South Wales",
            "country": "Australia",
            "precision": "approximate",
            "is_online": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["location"]["id"]


def _seed_region_fixtures(db_transaction, isolated_client) -> dict[str, object]:
    user_id, _username = seed_user(db_transaction, username_prefix="region-map")
    _channel_id, channel_slug = seed_channel_with_membership(db_transaction, creator_id=user_id)
    db_transaction.flush()

    token = create_access_token(str(user_id))
    headers = _auth_header(token)
    scheduled_at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    melbourne_location_id = _create_location(
        isolated_client,
        headers,
        label="Melbourne CBD, Victoria, Australia",
        lat=-37.8136,
        lon=144.9631,
    )
    sydney_location_id = _create_location(
        isolated_client,
        headers,
        label="Sydney CBD, New South Wales, Australia",
        lat=-33.8688,
        lon=151.2093,
    )
    private_location_id = _create_location(
        isolated_client,
        headers,
        label="Private Melbourne Venue, Victoria, Australia",
        lat=-37.82,
        lon=144.97,
    )

    near_event = isolated_client.post(
        "/events",
        headers=headers,
        json={
            "title": "Melbourne Region Event",
            "description": "Public event in Melbourne",
            "audience": "public",
            "governance": "collaborative",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "time_label": "Soon",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": melbourne_location_id,
            "scheduled_at": scheduled_at,
        },
    )
    assert near_event.status_code == 200, near_event.text
    near_slug = near_event.json()["event"]["slug"]

    far_event = isolated_client.post(
        "/events",
        headers=headers,
        json={
            "title": "Sydney Region Event",
            "description": "Public event in Sydney",
            "audience": "public",
            "governance": "collaborative",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "time_label": "Soon",
            "location_label": "Sydney CBD, New South Wales, Australia",
            "location_id": sydney_location_id,
            "scheduled_at": scheduled_at,
        },
    )
    assert far_event.status_code == 200, far_event.text
    far_slug = far_event.json()["event"]["slug"]

    private_event = isolated_client.post(
        "/events",
        headers=headers,
        json={
            "title": "Private Melbourne Event",
            "description": "Invite-only nearby event",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
            "time_label": "Soon",
            "location_label": "Private Melbourne Venue, Victoria, Australia",
            "location_id": private_location_id,
            "scheduled_at": scheduled_at,
            **private_event_plan_fields(title="Private Melbourne Plan"),
        },
    )
    assert private_event.status_code == 200, private_event.text
    private_slug = private_event.json()["event"]["slug"]

    needed_at, _ = future_scheduled_at(hours=3)
    help_request = isolated_client.post(
        "/content/help-requests",
        headers=headers,
        json={
            "title": "Melbourne Help Request",
            "body": "Need help in Melbourne",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": melbourne_location_id,
            "needed_at": needed_at,
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "roles": [{"title": "Helper", "description": "Lend a hand", "slots": 2}],
        },
    )
    assert help_request.status_code == 200, help_request.text
    help_id = help_request.json()["help_request"]["id"]

    return {
        "owner_token": token,
        "channel_slug": channel_slug,
        "melbourne_location_id": melbourne_location_id,
        "near_slug": near_slug,
        "far_slug": far_slug,
        "private_slug": private_slug,
        "help_id": help_id,
        "help_title": "Melbourne Help Request",
    }


def test_haversine_known_distance():
    # Melbourne ↔ Sydney is roughly 700–720 km
    distance = haversine_km(-37.8136, 144.9631, -33.8688, 151.2093)
    assert 650 < distance < 800


def test_region_feed_distance_and_private_exclusion(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    response = isolated_client.get(
        "/feeds/region",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 25,
            "filter": "events",
            "sort": "recent",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    slugs = [item["slug"] for item in payload["items"]]
    assert seeded["near_slug"] in slugs
    assert seeded["far_slug"] not in slugs
    assert seeded["private_slug"] not in slugs
    for item in payload["items"]:
        assert item.get("distance_km") is not None
        assert item["distance_km"] <= 25


def test_map_markers_exclude_private_and_respect_radius(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "all",
            "window": "all",
        },
        headers=_auth_header(seeded["owner_token"]),
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    titles = {item["title"] for item in items}
    assert "Melbourne Region Event" in titles
    assert seeded["help_title"] in titles
    assert "Sydney Region Event" not in titles
    # Creator can see their own invite-only event on the map.
    assert "Private Melbourne Event" in titles
    for item in items:
        assert "latitude" in item
        assert item["distance_km"] <= 50

    anonymous = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "all",
            "window": "all",
        },
    )
    assert anonymous.status_code == 200, anonymous.text
    anonymous_titles = {item["title"] for item in anonymous.json()["items"]}
    assert "Private Melbourne Event" not in anonymous_titles


def test_map_markers_invite_only_visible_to_invitee_not_outsider(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    owner_headers = _auth_header(seeded["owner_token"])
    invitee_id, invitee_username = seed_user(db_transaction, username_prefix="invitee-map")
    outsider_id, _outsider_username = seed_user(db_transaction, username_prefix="outsider-map")
    db_transaction.flush()
    invitee_token = create_access_token(str(invitee_id))
    outsider_token = create_access_token(str(outsider_id))
    scheduled_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    invite_event = isolated_client.post(
        "/events",
        headers=owner_headers,
        json={
            "title": "Invite Only Map Event",
            "description": "Birthday planning with guests",
            "audience": "invite_only",
            "governance": "collaborative",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [invitee_username],
            "time_label": "Tonight",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "scheduled_at": scheduled_at,
            **private_event_plan_fields(title="Invite Map Plan"),
        },
    )
    assert invite_event.status_code == 200, invite_event.text
    assert invite_event.json()["event"]["governance"] == "collaborative"

    params = {
        "lat": -37.8136,
        "lon": 144.9631,
        "radius_km": 50,
        "filter": "events",
        "window": "all",
    }

    invitee_response = isolated_client.get(
        "/feeds/map-markers",
        params=params,
        headers=_auth_header(invitee_token),
    )
    assert invitee_response.status_code == 200, invitee_response.text
    invitee_titles = {item["title"] for item in invitee_response.json()["items"]}
    assert "Invite Only Map Event" in invitee_titles

    outsider_response = isolated_client.get(
        "/feeds/map-markers",
        params=params,
        headers=_auth_header(outsider_token),
    )
    assert outsider_response.status_code == 200, outsider_response.text
    outsider_titles = {item["title"] for item in outsider_response.json()["items"]}
    assert "Invite Only Map Event" not in outsider_titles


def test_invite_only_event_allows_organizer_controlled_governance(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    response = isolated_client.post(
        "/events",
        headers=headers,
        json={
            "title": "Organizer Controlled Invite Event",
            "description": "Creator keeps plan control",
            "audience": "invite_only",
            "governance": "organizer_controlled",
            "channel_slugs": [],
            "community_slugs": [],
            "invited_usernames": [],
            "time_label": "Soon",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            **private_event_plan_fields(title="Organizer Invite Plan"),
        },
    )
    assert response.status_code == 200, response.text
    event = response.json()["event"]
    assert event["audience"] == "invite_only"
    assert event["governance"] == "organizer_controlled"
    assert event["is_private"] is True
    assert event["current_phase_id"] == "activity"


def test_public_event_rejects_organizer_controlled_governance(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    response = isolated_client.post(
        "/events",
        headers=headers,
        json={
            "title": "Invalid Public Controlled Event",
            "description": "Should fail",
            "audience": "public",
            "governance": "organizer_controlled",
            "channel_slugs": [seeded["channel_slug"]],
            "community_slugs": [],
            "time_label": "Soon",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
        },
    )
    assert response.status_code == 422


def test_region_rejects_invalid_radius(isolated_client):
    too_small = isolated_client.get(
        "/feeds/region",
        params={"lat": -37.8, "lon": 144.9, "radius_km": 0},
    )
    assert too_small.status_code == 422

    too_large = isolated_client.get(
        "/feeds/region",
        params={"lat": -37.8, "lon": 144.9, "radius_km": 20_001},
    )
    assert too_large.status_code == 422


def test_region_accepts_global_radius(isolated_client):
    response = isolated_client.get(
        "/feeds/region",
        params={"lat": -37.8, "lon": 144.9, "radius_km": 20_000},
    )
    assert response.status_code == 200, response.text


def test_region_accepts_custom_radius(isolated_client):
    response = isolated_client.get(
        "/feeds/region",
        params={"lat": -37.8, "lon": 144.9, "radius_km": 75},
    )
    assert response.status_code == 200, response.text


def test_map_markers_upcoming_only_filters_past(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    past_time = datetime.now(UTC) - timedelta(days=2)
    db_transaction.execute(
        update(events).where(events.c.slug == seeded["near_slug"]).values(scheduled_at=past_time)
    )
    db_transaction.flush()

    params = {
        "lat": -37.8136,
        "lon": 144.9631,
        "radius_km": 50,
        "filter": "events",
        "window": "all",
    }
    upcoming_response = isolated_client.get(
        "/feeds/map-markers",
        params={**params, "upcoming_only": True},
        headers=_auth_header(seeded["owner_token"]),
    )
    assert upcoming_response.status_code == 200, upcoming_response.text
    upcoming_titles = {item["title"] for item in upcoming_response.json()["items"]}
    assert "Melbourne Region Event" not in upcoming_titles

    all_response = isolated_client.get(
        "/feeds/map-markers",
        params={**params, "upcoming_only": False},
        headers=_auth_header(seeded["owner_token"]),
    )
    assert all_response.status_code == 200, all_response.text
    all_titles = {item["title"] for item in all_response.json()["items"]}
    assert "Melbourne Region Event" in all_titles


def test_map_markers_include_project_activity_and_exclude_undated_project_entity(
    db_transaction, isolated_client
):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Melbourne Map Project",
            "description": "Project for map marker coverage",
            "project_mode": "productive",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_slug = project.json()["project"]["slug"]

    plan = isolated_client.post(
        f"/projects/{project_slug}/plans",
        headers=headers,
        json={
            "plan_type": "production",
            "title": "Production plan with location",
            "description": "Plan supplies a physical location but no schedule.",
            "demand_consideration_note": "Meets demand.",
            "location_id": seeded["melbourne_location_id"],
            "plan_payload": {
                "locationLabel": "Melbourne CBD, Victoria, Australia",
                "planPhases": [{"title": "Stage 1", "details": "Build"}],
                "valueConsiderationNotes": {},
            },
        },
    )
    assert plan.status_code == 200, plan.text
    plan_id = plan.json()["plan"]["id"]

    for _ in range(3):
        vote = isolated_client.post(
            f"/projects/{project_slug}/plans/{plan_id}/vote",
            headers=headers,
            json={"vote": "yes"},
        )
        assert vote.status_code == 200, vote.text

    activity = isolated_client.post(
        f"/projects/{project_slug}/activities",
        headers=headers,
        json={
            "title": "Melbourne Project Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "note": "Scheduled work session",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "all",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    titles = {item["title"] for item in items}

    assert "Melbourne Project Activity" in titles
    project_items = [item for item in items if item["title"] == "Melbourne Map Project"]
    assert len(project_items) == 1
    assert project_items[0]["id"] == project.json()["project"]["id"]
    assert project_items[0]["id"] != seeded["melbourne_location_id"]
    assert project_items[0]["entity_type"] == "project"
    assert project_items[0]["project_mode"] == "productive"
    assert project_items[0]["activity_source"] is None
    assert project_items[0]["subtitle"] == "Proposal"

    activity_items = [item for item in items if item["title"] == "Melbourne Project Activity"]
    assert len(activity_items) == 1
    assert activity_items[0]["id"] == activity.json()["activity"]["id"]
    assert activity_items[0]["parent_id"] == project.json()["project"]["id"]
    activity_href = f"/projects/{project_slug}?activity={activity_items[0]['id']}"
    assert activity_items[0]["href"] == activity_href
    assert activity_items[0]["parent_title"] == "Melbourne Map Project"
    assert activity_items[0]["activity_source"] == "project"
    assert activity_items[0]["project_mode"] == "productive"

    scheduled_times = [
        item["scheduled_at"] for item in items if item.get("scheduled_at") is not None
    ]
    assert scheduled_times == sorted(scheduled_times)


def test_channel_tagged_help_request_appears_on_public_feed(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    response = isolated_client.get(
        "/feeds/public",
        params={"window": "all", "filter": "help_requests", "sort": "recent", "limit": 50},
    )
    assert response.status_code == 200, response.text
    help_ids = [
        item["id"] for item in response.json()["items"] if item.get("entity_type") == "help_request"
    ]
    assert seeded["help_id"] in help_ids


def test_map_markers_distance_from_anchor_uses_search_center(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])

    melbourne_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "events",
            "window": "all",
        },
        headers=headers,
    )
    assert melbourne_response.status_code == 200, melbourne_response.text
    melbourne_item = next(
        item
        for item in melbourne_response.json()["items"]
        if item["title"] == "Melbourne Region Event"
    )
    melbourne_distance = melbourne_item["distance_km"]

    sydney_center_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -33.8688,
            "lon": 151.2093,
            "radius_km": 2000,
            "distance_from_lat": -37.8136,
            "distance_from_lon": 144.9631,
            "filter": "events",
            "window": "all",
        },
        headers=headers,
    )
    assert sydney_center_response.status_code == 200, sydney_center_response.text
    anchored_item = next(
        item
        for item in sydney_center_response.json()["items"]
        if item["title"] == "Melbourne Region Event"
    )
    assert anchored_item["distance_km"] == melbourne_distance


def test_map_markers_hide_full_personal_service_activity(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=7)).isoformat()

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Personal Service Map Project",
            "description": "Personal service coverage",
            "project_mode": "personal-service",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_slug = project.json()["project"]["slug"]

    activity = isolated_client.post(
        f"/projects/{project_slug}/activities",
        headers=headers,
        json={
            "title": "Personal Service Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "note": "One helper needed",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text
    activity_id = activity.json()["activity"]["id"]
    role_id = activity.json()["activity"]["roles"][0]["id"]

    open_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert open_response.status_code == 200, open_response.text
    open_titles = {item["title"] for item in open_response.json()["items"]}
    assert "Personal Service Activity" in open_titles

    commit = isolated_client.post(
        f"/projects/{project_slug}/activities/{activity_id}/commit",
        headers=headers,
        json={"role_id": role_id},
    )
    assert commit.status_code == 200, commit.text

    full_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert full_response.status_code == 200, full_response.text
    full_titles = {item["title"] for item in full_response.json()["items"]}
    assert "Personal Service Activity" not in full_titles


def test_region_feed_includes_project_activity(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Melbourne Feed Project",
            "description": "Project for regional feed coverage",
            "project_mode": "productive",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_slug = project.json()["project"]["slug"]

    activity = isolated_client.post(
        f"/projects/{project_slug}/activities",
        headers=headers,
        json={
            "title": "Melbourne Feed Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "note": "Scheduled work session",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text

    response = isolated_client.get(
        "/feeds/region",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    activity_items = [item for item in items if item.get("entity_type") == "project_activity"]
    assert any(item["title"] == "Melbourne Feed Activity" for item in activity_items)
    matched = next(item for item in activity_items if item["title"] == "Melbourne Feed Activity")
    assert matched["project_mode"] == "productive"
    assert matched["slug"] == project_slug
    assert matched.get("distance_km") is not None


def test_map_markers_productive_project_pin_without_activity(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Undated Productive Project",
            "description": "Project pin without scheduled activity",
            "project_mode": "productive",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    project_items = [
        item
        for item in response.json()["items"]
        if item["title"] == "Undated Productive Project" and item["entity_type"] == "project"
    ]
    assert len(project_items) == 1
    assert project_items[0]["project_mode"] == "productive"
    assert project_items[0]["subtitle"] == "Proposal"


def test_map_markers_personal_service_project_pin_when_accepting_requests(
    db_transaction, isolated_client
):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Personal Service Map Pin",
            "description": "Open schedule personal service",
            "project_mode": "personal-service",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    project_items = [
        item
        for item in response.json()["items"]
        if item["title"] == "Personal Service Map Pin" and item["entity_type"] == "project"
    ]
    assert len(project_items) == 1
    assert project_items[0]["subtitle"] == "Accepting requests"


def test_map_markers_collective_service_project_pin_when_accepting_requests(
    db_transaction, isolated_client
):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Collective Service Map Pin",
            "description": "Collective service accepting requests",
            "project_mode": "collective-service",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_id = project.json()["project"]["id"]
    db_transaction.execute(
        insert(project_service_request_settings).values(
            project_id=project_id,
            enabled=True,
            request_mode="both",
            allow_off_schedule_requests=True,
            summary="",
        )
    )
    db_transaction.flush()

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    project_items = [
        item
        for item in response.json()["items"]
        if item["title"] == "Collective Service Map Pin" and item["entity_type"] == "project"
    ]
    assert len(project_items) == 1
    assert project_items[0]["subtitle"] == "Accepting requests"


def test_map_markers_collective_service_shown_with_physical_location(
    db_transaction, isolated_client
):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Visible Collective Service",
            "description": "Physical location should pin without requests",
            "project_mode": "collective-service",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    titles = {item["title"] for item in response.json()["items"]}
    assert "Visible Collective Service" in titles


def test_map_markers_include_event_activity_and_inherit_parent_location(
    db_transaction, isolated_client
):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    shared_activity = isolated_client.post(
        f"/events/{seeded['near_slug']}/activities",
        headers=headers,
        json={
            "title": "Melbourne Event Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "note": "Same place as the event",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert shared_activity.status_code == 200, shared_activity.text

    inherited_activity = isolated_client.post(
        f"/events/{seeded['near_slug']}/activities",
        headers=headers,
        json={
            "title": "Inherited Location Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "note": "Uses the parent event location",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert inherited_activity.status_code == 200, inherited_activity.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "events",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    event_items = [item for item in items if item["title"] == "Melbourne Region Event"]
    assert len(event_items) == 1
    event_id = event_items[0]["id"]
    assert event_id != seeded["melbourne_location_id"]

    shared_items = [item for item in items if item["title"] == "Melbourne Event Activity"]
    assert len(shared_items) == 1
    assert shared_items[0]["id"] == shared_activity.json()["activity"]["id"]
    assert shared_items[0]["parent_id"] == event_id
    shared_href = f"/events/{seeded['near_slug']}?activity={shared_items[0]['id']}"
    assert shared_items[0]["href"] == shared_href

    inherited_items = [item for item in items if item["title"] == "Inherited Location Activity"]
    assert len(inherited_items) == 1
    assert inherited_items[0]["id"] == inherited_activity.json()["activity"]["id"]
    assert inherited_items[0]["parent_id"] == event_id
    assert inherited_items[0]["latitude"] == event_items[0]["latitude"]
    assert inherited_items[0]["longitude"] == event_items[0]["longitude"]


def test_map_markers_project_activity_inherits_parent_location(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Inherit Location Project",
            "description": "Parent location should nest child activities",
            "project_mode": "productive",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_slug = project.json()["project"]["slug"]
    project_id = project.json()["project"]["id"]

    activity = isolated_client.post(
        f"/projects/{project_slug}/activities",
        headers=headers,
        json={
            "title": "Inherited Project Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "note": "No location_id, inherit parent",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text

    response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    parent_items = [item for item in items if item["title"] == "Inherit Location Project"]
    child_items = [item for item in items if item["title"] == "Inherited Project Activity"]
    assert len(parent_items) == 1
    assert parent_items[0]["id"] == project_id
    assert len(child_items) == 1
    assert child_items[0]["id"] == activity.json()["activity"]["id"]
    assert child_items[0]["parent_id"] == project_id
    assert child_items[0]["latitude"] == parent_items[0]["latitude"]
    assert child_items[0]["longitude"] == parent_items[0]["longitude"]


def test_map_markers_hide_full_collective_service_activity(db_transaction, isolated_client):
    seeded = _seed_region_fixtures(db_transaction, isolated_client)
    headers = _auth_header(seeded["owner_token"])
    scheduled_at = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    ends_at = (datetime.now(UTC) + timedelta(hours=7)).isoformat()

    project = isolated_client.post(
        "/projects",
        headers=headers,
        json={
            "title": "Collective Service Activity Project",
            "description": "Collective service activity coverage",
            "project_mode": "collective-service",
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "channel_slugs": [seeded["channel_slug"]],
        },
    )
    assert project.status_code == 200, project.text
    project_slug = project.json()["project"]["slug"]

    activity = isolated_client.post(
        f"/projects/{project_slug}/activities",
        headers=headers,
        json={
            "title": "Collective Service Activity",
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "location_label": "Melbourne CBD, Victoria, Australia",
            "location_id": seeded["melbourne_location_id"],
            "note": "One helper needed",
            "role_requirements": [{"label": "Helper", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text
    activity_id = activity.json()["activity"]["id"]
    role_id = activity.json()["activity"]["roles"][0]["id"]

    open_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert open_response.status_code == 200, open_response.text
    open_titles = {item["title"] for item in open_response.json()["items"]}
    assert "Collective Service Activity" in open_titles

    commit = isolated_client.post(
        f"/projects/{project_slug}/activities/{activity_id}/commit",
        headers=headers,
        json={"role_id": role_id},
    )
    assert commit.status_code == 200, commit.text

    full_response = isolated_client.get(
        "/feeds/map-markers",
        params={
            "lat": -37.8136,
            "lon": 144.9631,
            "radius_km": 50,
            "filter": "projects",
            "window": "all",
        },
        headers=headers,
    )
    assert full_response.status_code == 200, full_response.text
    full_titles = {item["title"] for item in full_response.json()["items"]}
    assert "Collective Service Activity" not in full_titles
