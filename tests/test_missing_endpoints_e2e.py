from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import channels, event_memberships, events, project_memberships, projects, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    owner_id = uuid4()
    member_id = uuid4()
    target_id = uuid4()

    owner_name = f"owner-{str(owner_id)[:8]}"
    member_name = f"member-{str(member_id)[:8]}"
    target_name = f"target-{str(target_id)[:8]}"

    for user_id, username in [(owner_id, owner_name), (member_id, member_name), (target_id, target_name)]:
        db.execute(
            insert(users).values(
                id=user_id,
                username=username,
                email=f"{username}@t.invalid",
                password_hash="x",
                bio=username,
                created_at=now,
                updated_at=now,
            )
        )

    channel_id = uuid4()
    channel_slug = f"miss-{str(channel_id)[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Missing Endpoints Channel",
            description="seed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    project_id = uuid4()
    project_slug = f"miss-proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Missing Endpoints Project",
            description="seed",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="proposal",
            location_label="online",
            is_platform_tagged=False,
            is_closed=False,
            signal_count=0,
            vote_count=0,
            comment_count=0,
            member_count=2,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    for user_id in [owner_id, member_id]:
        db.execute(
            insert(project_memberships).values(
                project_id=project_id,
                user_id=user_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
            )
        )

    event_id = uuid4()
    event_slug = f"miss-event-{str(event_id)[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=event_slug,
            title="Missing Endpoints Event",
            description="seed",
            created_by=owner_id,
            is_private=False,
            current_phase_id="proposal",
            time_label="Soon",
            location_label="online",
            scheduled_at=now,
            vote_count=0,
            comment_count=0,
            going_count=0,
            member_count=2,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    for user_id in [owner_id, member_id]:
        db.execute(
            insert(event_memberships).values(
                event_id=event_id,
                user_id=user_id,
                role="member",
                joined_at=now,
            )
        )

    db.commit()
    db.close()

    return {
        "owner_token": create_access_token(str(owner_id)),
        "member_token": create_access_token(str(member_id)),
        "target_token": create_access_token(str(target_id)),
        "project_slug": project_slug,
        "event_slug": event_slug,
        "target_username": target_name,
    }


def run() -> None:
    seeded = _seed()

    with TestClient(app) as client:
        onboarding = client.get("/onboarding")
        assert onboarding.status_code == 200, onboarding.text
        assert onboarding.json()["title"] == "Login"

        project_value = client.post(
            f"/projects/{seeded['project_slug']}/values",
            headers=_auth_header(seeded["owner_token"]),
            json={"label": "Reliability"},
        )
        assert project_value.status_code == 200, project_value.text
        project_value_id = project_value.json()["value"]["id"]

        project_plan = client.post(
            f"/projects/{seeded['project_slug']}/plans",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "plan_type": "production",
                "title": "Project Plan",
                "description": "Details",
                "demand_consideration_note": "note",
                "plan_payload": {"planPhases": []},
            },
        )
        assert project_plan.status_code == 200, project_plan.text
        project_plan_id = project_plan.json()["plan"]["id"]

        project_value_vote = client.post(
            f"/projects/{seeded['project_slug']}/plans/{project_plan_id}/value-votes",
            headers=_auth_header(seeded["member_token"]),
            json={"value_id": project_value_id, "vote": "yes"},
        )
        assert project_value_vote.status_code == 200, project_value_vote.text
        assert project_value_vote.json()["ok"] is True

        event_value = client.post(
            f"/events/{seeded['event_slug']}/values",
            headers=_auth_header(seeded["owner_token"]),
            json={"label": "Inclusion"},
        )
        assert event_value.status_code == 200, event_value.text
        event_value_id = event_value.json()["value"]["id"]

        event_plan = client.post(
            f"/events/{seeded['event_slug']}/plans",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "title": "Event Plan",
                "description": "Details",
                "demand_consideration_note": "note",
                "location_label": "online",
                "schedule_payload": {"mode": "any-day"},
                "plan_payload": {"planPhases": []},
            },
        )
        assert event_plan.status_code == 200, event_plan.text
        event_plan_id = event_plan.json()["plan"]["id"]

        event_value_vote = client.post(
            f"/events/{seeded['event_slug']}/plans/{event_plan_id}/value-votes",
            headers=_auth_header(seeded["member_token"]),
            json={"value_id": event_value_id, "vote": "yes"},
        )
        assert event_value_vote.status_code == 200, event_value_vote.text
        assert event_value_vote.json()["ok"] is True

        phase_advance = client.post(
            f"/projects/{seeded['project_slug']}/phase-advance",
            headers=_auth_header(seeded["member_token"]),
            json={"close_note": ""},
        )
        assert phase_advance.status_code == 200, phase_advance.text
        assert phase_advance.json()["current_phase_id"] == "phase-2"

        project_update = client.post(
            f"/projects/{seeded['project_slug']}/updates",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Weekly update", "body": "Progress is on track."},
        )
        assert project_update.status_code == 200, project_update.text

        share_project = client.post(
            f"/projects/{seeded['project_slug']}/share",
            headers=_auth_header(seeded["owner_token"]),
            json={"username": seeded["target_username"]},
        )
        assert share_project.status_code == 200, share_project.text
        assert share_project.json()["ok"] is True

        share_event = client.post(
            f"/events/{seeded['event_slug']}/share",
            headers=_auth_header(seeded["owner_token"]),
            json={"username": seeded["target_username"]},
        )
        assert share_event.status_code == 200, share_event.text
        assert share_event.json()["ok"] is True

        target_notifications = client.get(
            "/notifications",
            headers=_auth_header(seeded["target_token"]),
        )
        assert target_notifications.status_code == 200, target_notifications.text
        kinds = {item["kind"] for item in target_notifications.json()["items"]}
        assert {"prj-share", "evt-share"}.issubset(kinds)

        print(
            json.dumps(
                {
                    "onboarding": onboarding.json()["title"],
                    "project_plan_value_vote": project_value_vote.json()["ok"],
                    "event_plan_value_vote": event_value_vote.json()["ok"],
                    "project_phase_after_advance": phase_advance.json()["current_phase_id"],
                    "project_update_created": bool(project_update.json().get("update")),
                    "project_share_ok": share_project.json()["ok"],
                    "event_share_ok": share_event.json()["ok"],
                }
            )
        )


if __name__ == "__main__":
    run()