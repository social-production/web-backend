from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import (
    channels,
    communities,
    project_memberships,
    projects,
    scope_invites,
    users,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    owner_id = uuid4()
    member_id = uuid4()
    extra_id = uuid4()

    owner_name = f"owner-{str(owner_id)[:8]}"
    member_name = f"member-{str(member_id)[:8]}"
    extra_name = f"extra-{str(extra_id)[:8]}"

    for user_id, username in [
        (owner_id, owner_name),
        (member_id, member_name),
        (extra_id, extra_name),
    ]:
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
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=f"wch-{str(channel_id)[:8]}",
            name="Writes Channel",
            description="seed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    community_id = uuid4()
    community_slug = f"wco-{str(community_id)[:8]}"
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=community_slug,
            name="Closed Community",
            description="seed",
            join_policy="closed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    invite_token = f"invite-{str(uuid4())}"
    invite_hash = hashlib.sha256(invite_token.encode("utf-8")).hexdigest()
    db.execute(
        insert(scope_invites).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=community_id,
            token_hash=invite_hash,
            created_by=owner_id,
            expires_at=now + timedelta(days=2),
            max_uses=3,
            uses=0,
            created_at=now,
        )
    )

    project_id = uuid4()
    project_slug = f"writes-proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Writes Test Project",
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
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=owner_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=member_id,
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
        "extra_token": create_access_token(str(extra_id)),
        "member_username": member_name,
        "extra_username": extra_name,
        "project_id": str(project_id),
        "project_slug": project_slug,
        "invite_token": invite_token,
        "community_slug": community_slug,
    }


def run() -> None:
    seeded = _seed()

    with TestClient(app) as client:
        group = client.post(
            "/messages/group",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Ops Group", "participant_usernames": [seeded["member_username"]]},
        )
        assert group.status_code == 200, group.text
        conversation_id = group.json()["conversation"]["id"]

        renamed = client.patch(
            f"/messages/conversations/{conversation_id}",
            headers=_auth_header(seeded["owner_token"]),
            json={"title": "Ops Group Renamed"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["conversation"]["title"] == "Ops Group Renamed"

        added = client.post(
            f"/messages/conversations/{conversation_id}/members",
            headers=_auth_header(seeded["owner_token"]),
            json={"username": seeded["extra_username"]},
        )
        assert added.status_code == 200, added.text

        removed = client.delete(
            f"/messages/conversations/{conversation_id}/members/{seeded['member_username']}",
            headers=_auth_header(seeded["owner_token"]),
        )
        assert removed.status_code == 200, removed.text

        report = client.post(
            "/governance/reports",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "target_type": "project",
                "target_id": seeded["project_id"],
                "reason": "spam",
                "description": "Looks like spam content",
            },
        )
        assert report.status_code == 200, report.text
        report_id = report.json()["report"]["id"]

        report_vote = client.post(
            f"/governance/reports/{report_id}/vote",
            headers=_auth_header(seeded["member_token"]),
            json={"vote": "yes"},
        )
        assert report_vote.status_code == 200, report_vote.text

        redeemed = client.post(
            "/scopes/invites/redeem",
            headers=_auth_header(seeded["member_token"]),
            json={"token": seeded["invite_token"]},
        )
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.json()["slug"] == seeded["community_slug"]

        value_resp = client.post(
            f"/projects/{seeded['project_slug']}/values",
            headers=_auth_header(seeded["owner_token"]),
            json={"label": "Durability"},
        )
        assert value_resp.status_code == 200, value_resp.text
        value_id = value_resp.json()["value"]["id"]

        value_vote = client.post(
            f"/projects/{seeded['project_slug']}/values/{value_id}/importance",
            headers=_auth_header(seeded["member_token"]),
            json={"importance": 8},
        )
        assert value_vote.status_code == 200, value_vote.text

        activity_now = datetime.now(timezone.utc)
        activity = client.post(
            f"/projects/{seeded['project_slug']}/activities",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "title": "Build session",
                "scheduled_at": (activity_now + timedelta(hours=1)).isoformat(),
                "ends_at": (activity_now + timedelta(hours=3)).isoformat(),
                "location_label": "Workshop",
                "note": "Bring tools",
                "role_requirements": [
                    {
                        "label": "Builder",
                        "required_count": 1,
                        "maximum_count": 2,
                    }
                ],
            },
        )
        assert activity.status_code == 200, activity.text
        activity_id = activity.json()["activity"]["id"]
        role_id = activity.json()["activity"]["roles"][0]["id"]

        commit = client.post(
            f"/projects/{seeded['project_slug']}/activities/{activity_id}/commit",
            headers=_auth_header(seeded["member_token"]),
            json={"role_id": role_id},
        )
        assert commit.status_code == 200, commit.text

        print(
            json.dumps(
                {
                    "group_conversation_management": True,
                    "content_moderation_report_and_vote": True,
                    "scope_invite_redemption": True,
                    "project_values_and_importance_vote": True,
                    "project_activity_and_role_commit": True,
                    "report_resolution": report_vote.json()["report"]["resolution"],
                    "activity_commit_ok": commit.json()["ok"],
                }
            )
        )


if __name__ == "__main__":
    run()
