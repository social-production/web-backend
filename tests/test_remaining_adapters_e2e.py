from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import event_memberships, events, project_memberships, projects, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    owner_id = uuid4()
    member_id = uuid4()

    owner_name = f"owner-{str(owner_id)[:8]}"
    member_name = f"member-{str(member_id)[:8]}"

    for user_id, username in [(owner_id, owner_name), (member_id, member_name)]:
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

    event_id = uuid4()
    event_slug = f"rem-event-{str(event_id)[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=event_slug,
            title="Remaining Adapters Event",
            description="seed",
            created_by=owner_id,
            is_private=True,
            current_phase_id="phase-1",
            time_label="Soon",
            location_label="Workshop",
            scheduled_at=now + timedelta(days=1),
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

    project_id = uuid4()
    project_slug = f"rem-proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Remaining Adapters Project",
            description="seed",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-2",
            stage_label="production-plan",
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

    db.commit()
    db.close()

    return {
        "owner_token": create_access_token(str(owner_id)),
        "member_token": create_access_token(str(member_id)),
        "member_id": str(member_id),
        "member_username": member_name,
        "event_slug": event_slug,
        "project_slug": project_slug,
    }


def run() -> None:
    seeded = _seed()

    with TestClient(app) as client:
        direct = client.post(
            "/messages/direct",
            headers=_auth_header(seeded["owner_token"]),
            json={"other_username": seeded["member_username"]},
        )
        assert direct.status_code == 200, direct.text

        grant = client.post(
            f"/events/{seeded['event_slug']}/editors/grant",
            headers=_auth_header(seeded["owner_token"]),
            json={"user_id": seeded["member_id"]},
        )
        assert grant.status_code == 200, grant.text

        revoke = client.post(
            f"/events/{seeded['event_slug']}/editors/revoke",
            headers=_auth_header(seeded["owner_token"]),
            json={"user_id": seeded["member_id"]},
        )
        assert revoke.status_code == 200, revoke.text

        event_value = client.post(
            f"/events/{seeded['event_slug']}/values",
            headers=_auth_header(seeded["owner_token"]),
            json={"label": "Care"},
        )
        assert event_value.status_code == 200, event_value.text
        event_value_id = event_value.json()["value"]["id"]

        event_value_vote = client.post(
            f"/events/{seeded['event_slug']}/values/{event_value_id}/importance",
            headers=_auth_header(seeded["member_token"]),
            json={"importance": 9},
        )
        assert event_value_vote.status_code == 200, event_value_vote.text

        activity = client.post(
            f"/events/{seeded['event_slug']}/activities",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "title": "Setup session",
                "scheduled_at": datetime.now(timezone.utc).isoformat(),
                "ends_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "location_label": "Hall A",
                "note": "Bring checklists",
                "role_requirements": [{"label": "Host", "required_count": 1, "maximum_count": 2}],
            },
        )
        assert activity.status_code == 200, activity.text
        event_activity_id = activity.json()["activity"]["id"]
        event_role_id = activity.json()["activity"]["roles"][0]["id"]

        event_commit = client.post(
            f"/events/{seeded['event_slug']}/activities/{event_activity_id}/commit",
            headers=_auth_header(seeded["member_token"]),
            json={"role_id": event_role_id},
        )
        assert event_commit.status_code == 200, event_commit.text

        update_request = client.post(
            f"/projects/{seeded['project_slug']}/update-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"body": "Add timeline details"},
        )
        assert update_request.status_code == 200, update_request.text
        update_request_id = update_request.json()["request"]["id"]

        update_vote_owner = client.post(
            f"/projects/{seeded['project_slug']}/update-requests/{update_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert update_vote_owner.status_code == 200, update_vote_owner.text
        update_vote_member = client.post(
            f"/projects/{seeded['project_slug']}/update-requests/{update_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert update_vote_member.status_code == 200, update_vote_member.text

        edit_request = client.post(
            f"/projects/{seeded['project_slug']}/edit-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Polish description", "description": "Tighten wording in overview"},
        )
        assert edit_request.status_code == 200, edit_request.text
        edit_request_id = edit_request.json()["request"]["id"]

        edit_vote_owner = client.post(
            f"/projects/{seeded['project_slug']}/edit-requests/{edit_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert edit_vote_owner.status_code == 200, edit_vote_owner.text
        edit_vote_member = client.post(
            f"/projects/{seeded['project_slug']}/edit-requests/{edit_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert edit_vote_member.status_code == 200, edit_vote_member.text

        revert_request = client.post(
            f"/projects/{seeded['project_slug']}/revert-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"target_phase_id": "phase-1", "reason": "Need additional prep"},
        )
        assert revert_request.status_code == 200, revert_request.text
        revert_request_id = revert_request.json()["request"]["id"]

        revert_vote_owner = client.post(
            f"/projects/{seeded['project_slug']}/revert-requests/{revert_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert revert_vote_owner.status_code == 200, revert_vote_owner.text
        revert_vote_member = client.post(
            f"/projects/{seeded['project_slug']}/revert-requests/{revert_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert revert_vote_member.status_code == 200, revert_vote_member.text

        send = client.post(
            f"/messages/conversations/{direct.json()['conversation']['id']}/messages",
            headers=_auth_header(seeded["member_token"]),
            json={"body": "ping"},
        )
        assert send.status_code == 200, send.text

        mark_read = client.post(
            f"/messages/conversations/{direct.json()['conversation']['id']}/read",
            headers=_auth_header(seeded["owner_token"]),
            json={},
        )
        assert mark_read.status_code == 200, mark_read.text

        print(
            json.dumps(
                {
                    "event_editor_management": grant.json()["ok"] and revoke.json()["ok"],
                    "event_values_and_importance_vote": event_value_vote.json()["ok"],
                    "event_activity_and_role_commit": event_commit.json()["ok"],
                    "project_update_request_vote_executed": update_vote_member.json()["executed"],
                    "project_edit_request_vote_executed": edit_vote_member.json()["executed"],
                    "project_revert_vote_executed": revert_vote_member.json()["executed"],
                    "project_phase_after_revert": revert_vote_member.json()["current_phase_id"],
                    "conversation_mark_read_ok": mark_read.json()["ok"],
                }
            )
        )


if __name__ == "__main__":
    run()
