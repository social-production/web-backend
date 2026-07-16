from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import channels, meaningful_actions, notifications, searchable_documents, users


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(UTC)

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

    channel_id = uuid4()
    channel_slug = f"act-{str(channel_id)[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Actions Channel",
            description="seed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    db.commit()
    db.close()

    return {
        "owner_id": owner_id,
        "member_id": member_id,
        "owner_token": create_access_token(str(owner_id)),
        "member_token": create_access_token(str(member_id)),
        "channel_slug": channel_slug,
    }


def run() -> None:
    seeded = _seed()

    project_slug = f"act-proj-{str(uuid4())[:8]}"
    event_slug = f"act-event-{str(uuid4())[:8]}"

    with TestClient(app) as client:
        project_create = client.post(
            "/projects",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "slug": project_slug,
                "title": "Actions Project",
                "description": "Project before edit",
                "project_mode": "productive",
                "project_subtype": "software",
                "location_label": "online",
                "channel_slugs": [seeded["channel_slug"]],
            },
        )
        assert project_create.status_code == 200, project_create.text
        project_id = UUID(project_create.json()["project"]["id"])

        event_create = client.post(
            "/events",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "slug": event_slug,
                "title": "Actions Event",
                "description": "Event before edit",
                "is_private": False,
                "time_label": "Soon",
                "location_label": "Online",
                "channel_slugs": [seeded["channel_slug"]],
            },
        )
        assert event_create.status_code == 200, event_create.text
        event_id = UUID(event_create.json()["event"]["id"])

        join_project = client.post(
            f"/projects/{project_slug}/join",
            headers=_auth_header(seeded["member_token"]),
        )
        assert join_project.status_code == 200, join_project.text

        join_event = client.post(
            f"/events/{event_slug}/join",
            headers=_auth_header(seeded["member_token"]),
        )
        assert join_event.status_code == 200, join_event.text

        project_signal = client.post(
            f"/projects/{project_slug}/signals",
            headers=_auth_header(seeded["member_token"]),
            json={"signal_type": "demand"},
        )
        assert project_signal.status_code == 200, project_signal.text

        event_signal = client.post(
            f"/events/{event_slug}/signals",
            headers=_auth_header(seeded["member_token"]),
            json={"signal_type": "demand"},
        )
        assert event_signal.status_code == 200, event_signal.text

        post_create = client.post(
            "/content/posts",
            headers=_auth_header(seeded["owner_token"]),
            json={"body": "Action post", "audience": "public"},
        )
        assert post_create.status_code == 200, post_create.text
        post_id = post_create.json()["post"]["id"]

        comment_create = client.post(
            "/governance/comments",
            headers=_auth_header(seeded["member_token"]),
            json={"subject_type": "post", "subject_id": post_id, "body": "Action comment"},
        )
        assert comment_create.status_code == 200, comment_create.text

        content_vote = client.post(
            "/governance/votes",
            headers=_auth_header(seeded["owner_token"]),
            json={"target_type": "post", "target_id": post_id, "direction": "up"},
        )
        assert content_vote.status_code == 200, content_vote.text

        event_plan = client.post(
            f"/events/{event_slug}/plans",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "title": "Plan",
                "description": "Plan body",
                "demand_consideration_note": "note",
                "location_label": "Online",
                "schedule_payload": {"window": "today"},
                "plan_payload": {"steps": 2},
            },
        )
        assert event_plan.status_code == 200, event_plan.text
        event_plan_id = event_plan.json()["plan"]["id"]

        vote_event_plan_owner = client.post(
            f"/events/{event_slug}/plans/{event_plan_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert vote_event_plan_owner.status_code == 200, vote_event_plan_owner.text
        vote_event_plan_member = client.post(
            f"/events/{event_slug}/plans/{event_plan_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert vote_event_plan_member.status_code == 200, vote_event_plan_member.text
        assert vote_event_plan_member.json()["is_leading"] is True

        event_phase_request = client.post(
            f"/events/{event_slug}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"target_phase_id": "event-plan", "reason": "Ready"},
        )
        assert event_phase_request.status_code == 200, event_phase_request.text
        event_phase_request_id = event_phase_request.json()["request"]["id"]

        vote_phase_owner = client.post(
            f"/events/{event_slug}/phase-requests/{event_phase_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert vote_phase_owner.status_code == 200, vote_phase_owner.text
        vote_phase_member = client.post(
            f"/events/{event_slug}/phase-requests/{event_phase_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert vote_phase_member.status_code == 200, vote_phase_member.text
        assert vote_phase_member.json()["executed"] is True

        project_edit_request = client.post(
            f"/projects/{project_slug}/edit-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Actions Project Edited", "description": "Project after edit"},
        )
        assert project_edit_request.status_code == 200, project_edit_request.text
        project_edit_request_id = project_edit_request.json()["request"]["id"]

        project_edit_vote_owner = client.post(
            f"/projects/{project_slug}/edit-requests/{project_edit_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert project_edit_vote_owner.status_code == 200, project_edit_vote_owner.text
        project_edit_vote_member = client.post(
            f"/projects/{project_slug}/edit-requests/{project_edit_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert project_edit_vote_member.status_code == 200, project_edit_vote_member.text
        assert project_edit_vote_member.json()["executed"] is True

        event_edit_request = client.post(
            f"/events/{event_slug}/edit-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Actions Event Edited", "description": "Event after edit"},
        )
        assert event_edit_request.status_code == 200, event_edit_request.text
        event_edit_request_id = event_edit_request.json()["request"]["id"]

        event_edit_vote_owner = client.post(
            f"/events/{event_slug}/edit-requests/{event_edit_request_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert event_edit_vote_owner.status_code == 200, event_edit_vote_owner.text
        event_edit_vote_member = client.post(
            f"/events/{event_slug}/edit-requests/{event_edit_request_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert event_edit_vote_member.status_code == 200, event_edit_vote_member.text
        assert event_edit_vote_member.json()["executed"] is True

        pull_request_create = client.post(
            f"/projects/{project_slug}/software/pull-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "title": "PR 1",
                "summary": "Implements feature",
                "pullRequestId": "42",
                "pullRequestUrl": "https://example.invalid/pr/42",
            },
        )
        assert pull_request_create.status_code == 200, pull_request_create.text
        pr_id = pull_request_create.json()["pullRequests"][0]["id"]

        pr_vote_owner = client.post(
            f"/projects/{project_slug}/software/pull-requests/{pr_id}/vote",
            headers=_auth_header(seeded["owner_token"]),
            json={"vote": "yes"},
        )
        assert pr_vote_owner.status_code == 200, pr_vote_owner.text
        pr_vote_member = client.post(
            f"/projects/{project_slug}/software/pull-requests/{pr_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert pr_vote_member.status_code == 200, pr_vote_member.text

    db = SessionLocal()

    action_rows = db.execute(
        select(meaningful_actions.c.action_type).where(
            meaningful_actions.c.user_id.in_([seeded["owner_id"], seeded["member_id"]])
        )
    ).all()
    action_types = {row[0] for row in action_rows}

    project_doc = db.execute(
        select(searchable_documents.c.title).where(
            searchable_documents.c.entity_type == "project",
            searchable_documents.c.entity_id == project_id,
        )
    ).first()
    event_doc = db.execute(
        select(searchable_documents.c.title).where(
            searchable_documents.c.entity_type == "event",
            searchable_documents.c.entity_id == event_id,
        )
    ).first()

    owner_notifications = db.execute(
        select(notifications.c.kind).where(notifications.c.recipient_id == seeded["owner_id"])
    ).all()
    owner_notification_kinds = {row[0] for row in owner_notifications}

    db.close()

    expected_actions = {
        "cast-vote",
        "create-post",
        "create-comment",
        "signal-demand",
        "join-project",
        "join-event",
    }
    assert expected_actions.issubset(action_types), {
        "missing_actions": sorted(expected_actions - action_types)
    }

    assert project_doc is not None and project_doc[0] == "Actions Project Edited"
    assert event_doc is not None and event_doc[0] == "Actions Event Edited"

    expected_notifications = {"evt-plan-lead", "evt-phase-done", "pr-approved"}
    assert expected_notifications.issubset(owner_notification_kinds), {
        "missing_notifications": sorted(expected_notifications - owner_notification_kinds)
    }

    print(
        json.dumps(
            {
                "meaningful_actions_logged": sorted(expected_actions),
                "project_search_title": project_doc[0],
                "event_search_title": event_doc[0],
                "notification_kinds_verified": sorted(expected_notifications),
            }
        )
    )


if __name__ == "__main__":
    run()
