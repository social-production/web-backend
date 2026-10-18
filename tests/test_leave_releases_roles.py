"""Leaving a project frees your upcoming roles, and organizers hear about roles and leavers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth.jwt import create_access_token
from app.models import notifications, project_activity_assignments
from tests.conftest import seed_channel_with_membership, seed_user, set_project_phase


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def test_leaving_releases_upcoming_roles_and_tells_the_organizer(db_transaction, isolated_client):
    owner_id, _ = seed_user(db_transaction, username_prefix="leave-owner")
    member_id, member_name = seed_user(db_transaction, username_prefix="leave-member")
    _channel_id, channel_slug = seed_channel_with_membership(db_transaction, creator_id=owner_id)
    db_transaction.flush()

    created = isolated_client.post(
        "/projects",
        headers=_auth(owner_id),
        json={
            "title": "Community garden",
            "description": "Grow food together",
            "project_mode": "productive",
            "location_label": "Online",
            "channel_slugs": [channel_slug],
        },
    )
    assert created.status_code == 200, created.text
    slug = created.json()["project"]["slug"]
    set_project_phase(db_transaction, slug, "phase-5")
    joined = isolated_client.post(f"/projects/{slug}/join", headers=_auth(member_id))
    assert joined.status_code == 200, joined.text

    activity = isolated_client.post(
        f"/projects/{slug}/activities",
        headers=_auth(owner_id),
        json={
            "title": "Planting day",
            "scheduled_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "ends_at": (datetime.now(UTC) + timedelta(days=2, hours=3)).isoformat(),
            "location_label": "Garden",
            "note": "Bring gloves",
            "role_requirements": [{"label": "Planter", "required_count": 1}],
        },
    )
    assert activity.status_code == 200, activity.text
    activity_id = activity.json()["activity"]["id"]
    role_id = activity.json()["activity"]["roles"][0]["id"]

    took = isolated_client.post(
        f"/projects/{slug}/activities/{activity_id}/commit",
        headers=_auth(member_id),
        json={"role_id": role_id},
    )
    assert took.status_code == 200, took.text

    def owner_notices():
        return list(
            db_transaction.execute(
                select(notifications.c.kind, notifications.c.body).where(
                    notifications.c.recipient_id == owner_id
                )
            ).all()
        )

    assert ("prj-role-taken", f'{member_name} took the "Planter" role in Planting day.') in (
        owner_notices()
    )

    left = isolated_client.delete(f"/projects/{slug}/leave", headers=_auth(member_id))
    assert left.status_code == 200, left.text

    still_assigned = db_transaction.execute(
        select(project_activity_assignments.c.user_id).where(
            project_activity_assignments.c.role_id == role_id
        )
    ).all()
    assert still_assigned == []
    assert any(
        kind == "prj-member-left"
        and body.startswith(f"{member_name} left Community garden.")
        and "open again" in body
        for kind, body in owner_notices()
    )
