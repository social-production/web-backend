from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import (
    board_standing_votes,
    channels,
    communities,
    conversation_members,
    conversations,
    event_tags,
    events,
    meaningful_actions,
    notifications,
    platform_board_memberships,
    posts,
    projects,
    scope_memberships,
    user_follows,
    users,
)


def _request_json(
    url: str, method: str = "GET", body: dict[str, object] | None = None, token: str | None = None
) -> dict[str, object]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed_test_data() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    viewer = uuid4()
    followed = uuid4()
    candidate = uuid4()
    member = uuid4()
    contact = uuid4()
    voter = uuid4()

    platform_channel = uuid4()
    member_channel = uuid4()
    member_community = uuid4()
    conversation_id = uuid4()

    for user_id, name in [
        (viewer, "viewer"),
        (followed, "followed"),
        (candidate, "candidate"),
        (member, "member"),
        (contact, "contact"),
        (voter, "voter"),
    ]:
        db.execute(
            insert(users).values(
                id=user_id,
                username=f"{name}-{str(user_id)[:8]}",
                email=f"{name}-{str(user_id)[:8]}@t.invalid",
                password_hash="x",
                bio=f"{name} bio",
                created_at=now,
                updated_at=now,
            )
        )

    existing_platform_channel = (
        db.execute(select(channels.c.id).where(channels.c.slug == "platform")).mappings().first()
    )
    if existing_platform_channel is None:
        db.execute(
            insert(channels).values(
                id=platform_channel,
                slug="platform",
                name="Platform",
                description="Platform channel",
                created_by=viewer,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        platform_channel = existing_platform_channel["id"]
    db.execute(
        insert(channels).values(
            id=member_channel,
            slug=f"ch-{str(member_channel)[:8]}",
            name="Member Channel",
            description="Member channel",
            created_by=viewer,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(communities).values(
            id=member_community,
            slug=f"co-{str(member_community)[:8]}",
            name="Member Community",
            description="Member community",
            join_policy="open",
            created_by=viewer,
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="channel",
            scope_id=member_channel,
            user_id=viewer,
            role="member",
            created_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=member_community,
            user_id=viewer,
            role="member",
            created_at=now,
        )
    )

    db.execute(
        insert(user_follows).values(
            follower_id=viewer,
            followed_id=followed,
            status="accepted",
            created_at=now,
        )
    )

    db.execute(
        insert(notifications).values(
            id=uuid4(),
            recipient_id=viewer,
            actor_id=followed,
            kind="mention",
            surface="messages",
            subject_type="thread",
            subject_id=uuid4(),
            target_id=None,
            title="n1",
            body="n1",
            href="/threads/x",
            is_unread=True,
            created_at=now,
            read_at=None,
        )
    )

    db.execute(
        insert(conversations).values(
            id=conversation_id,
            kind="direct",
            title=None,
            created_by=viewer,
            created_at=now,
            updated_at=now,
            last_message_at=now,
        )
    )
    db.execute(
        insert(conversation_members).values(
            conversation_id=conversation_id,
            user_id=viewer,
            joined_at=now,
            last_read_at=None,
        )
    )

    db.execute(
        insert(posts).values(
            id=uuid4(),
            author_id=followed,
            body="followed post body",
            audience="public",
            vote_count=3,
            comment_count=1,
            created_at=now,
            updated_at=now,
        )
    )

    project_slug = f"platproj-{str(uuid4())[:8]}"
    event_slug = f"platevent-{str(uuid4())[:8]}"

    db.execute(
        insert(projects).values(
            id=uuid4(),
            slug=project_slug,
            title="Platform Tagged Project",
            description="project body",
            author_id=followed,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="early",
            location_label="remote",
            is_platform_tagged=True,
            is_closed=False,
            signal_count=2,
            vote_count=4,
            comment_count=1,
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )

    event_id = uuid4()
    db.execute(
        insert(events).values(
            id=event_id,
            slug=event_slug,
            title="Platform Event",
            description="event body",
            created_by=followed,
            is_private=False,
            current_phase_id="phase-1",
            time_label="soon",
            location_label="online",
            vote_count=2,
            comment_count=1,
            going_count=1,
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(event_tags).values(
            id=uuid4(),
            event_id=event_id,
            tag_kind="channel",
            channel_id=platform_channel,
            community_id=None,
        )
    )

    db.execute(
        insert(platform_board_memberships).values(
            user_id=member,
            standing_state="member",
            grace_started_at=None,
            grace_ends_at=None,
            updated_at=now,
        )
    )
    db.execute(
        insert(platform_board_memberships).values(
            user_id=candidate,
            standing_state="candidate",
            grace_started_at=None,
            grace_ends_at=None,
            updated_at=now,
        )
    )
    db.execute(
        insert(board_standing_votes).values(
            target_user_id=member,
            voter_id=voter,
            vote=1,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(board_standing_votes).values(
            target_user_id=member,
            voter_id=viewer,
            vote=1,
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(meaningful_actions).values(
            id=uuid4(),
            user_id=viewer,
            action_type="test",
            occurred_at=now,
            metadata={},
        )
    )
    db.execute(
        insert(meaningful_actions).values(
            id=uuid4(),
            user_id=voter,
            action_type="test",
            occurred_at=now,
            metadata={},
        )
    )

    db.commit()
    db.close()

    return {
        "viewer_token": create_access_token(str(viewer)),
        "project_slug": project_slug,
        "event_slug": event_slug,
    }


def run() -> None:
    base = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8009")
    seeded = seed_test_data()

    bootstrap = _request_json(f"{base}/bootstrap", token=seeded["viewer_token"])
    assert bootstrap["viewer"]["id"]
    assert bootstrap["featureFlags"] == {"assets": False, "funding": False, "platform": True}
    assert bootstrap["unreadCounts"]["notifications"] >= 1
    assert isinstance(bootstrap["unreadCounts"]["messages"], int)
    assert bootstrap["directory"]["platform"]["slug"] == "platform"
    assert len(bootstrap["directory"]["channels"]) >= 1
    assert len(bootstrap["directory"]["communities"]) >= 1

    personal = _request_json(
        f"{base}/feeds/personal?sort=recent&limit=20&offset=0",
        token=seeded["viewer_token"],
    )
    assert personal["total"] >= 2
    personal_types = {item["entity_type"] for item in personal["items"]}
    assert "post" in personal_types
    assert len(personal_types.intersection({"project", "thread", "event"})) >= 1

    platform_public = _request_json(
        f"{base}/platform?sort=recent&limit=20&offset=0",
    )
    assert platform_public["feed"]["total"] >= 2
    public_slugs = {item["slug"] for item in platform_public["feed"]["items"]}
    assert seeded["project_slug"] in public_slugs
    assert seeded["event_slug"] in public_slugs
    assert platform_public["board_candidacy_options"] is None

    platform_authed = _request_json(
        f"{base}/platform?sort=recent&limit=20&offset=0",
        token=seeded["viewer_token"],
    )
    assert platform_authed["board_candidacy_options"] is not None

    print(
        json.dumps(
            {
                "bootstrap_ok": True,
                "personal_feed_ok": True,
                "platform_public_ok": True,
                "platform_authed_ok": True,
                "personal_total": personal["total"],
                "platform_total": platform_public["feed"]["total"],
            }
        )
    )


if __name__ == "__main__":
    run()
