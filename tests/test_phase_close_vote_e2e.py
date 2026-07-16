from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import event_memberships, events, project_memberships, projects, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    owner_id = uuid4()
    member_id = uuid4()
    owner_name = f"close-owner-{str(owner_id)[:8]}"
    member_name = f"close-member-{str(member_id)[:8]}"

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

    project_id = uuid4()
    project_slug = f"close-proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Close Vote Project",
            description="seed",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="activity",
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
    event_slug = f"close-evt-{str(event_id)[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=event_slug,
            title="Close Vote Event",
            description="seed",
            created_by=owner_id,
            is_private=True,
            current_phase_id="activity",
            time_label="Soon",
            location_label="Workshop",
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
        "project_slug": project_slug,
        "event_slug": event_slug,
    }


def run() -> None:
    seeded = _seed()

    with TestClient(app) as client:
        project_close = client.post(
            f"/projects/{seeded['project_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"target_phase_id": "phase-7", "reason": "Project complete."},
        )
        assert project_close.status_code == 200, project_close.text
        project_request = project_close.json()["request"]
        assert project_request["change_kind"] == "close"

        event_close = client.post(
            f"/events/{seeded['event_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"target_phase_id": "closed", "reason": "Event finished."},
        )
        assert event_close.status_code == 200, event_close.text
        event_request = event_close.json()["request"]
        assert event_request["change_kind"] == "close"

        project_detail = client.get(f"/projects/{seeded['project_slug']}")
        assert project_detail.status_code == 200, project_detail.text
        open_requests = project_detail.json()["lifecycle"]["phaseChangeRequests"]
        close_requests = [item for item in open_requests if item["targetPhaseId"] == "phase-7"]
        assert close_requests, project_detail.text
        assert close_requests[0]["kind"] == "close"

    print("test_phase_close_vote_e2e: ok")


if __name__ == "__main__":
    run()
