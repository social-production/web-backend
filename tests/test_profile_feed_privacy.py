"""Someone's profile feed doesn't show what they do in private communities you aren't in."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import comments, communities, project_tags, projects, scope_memberships
from tests.conftest import seed_channel_with_membership, seed_user


def _project(db, author_id, title, now) -> tuple:
    project_id = uuid4()
    slug = f"pf-{uuid4().hex[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title=title,
            description="test",
            author_id=author_id,
            project_mode="productive",
            current_phase_id="phase-1",
            stage_label="early",
            location_label="Somewhere",
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    return project_id, slug


def _tag(db, project_id, *, community_id=None, channel_id=None) -> None:
    db.execute(
        insert(project_tags).values(
            id=uuid4(),
            project_id=project_id,
            tag_kind="community" if community_id else "channel",
            community_id=community_id,
            channel_id=channel_id,
        )
    )


def test_profile_feed_hides_private_community_activity_from_outsiders() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    author_id, author_name = seed_user(db, username_prefix="pf-author")
    member_id, _ = seed_user(db, username_prefix="pf-member")
    outsider_id, _ = seed_user(db, username_prefix="pf-outsider")
    channel_id, _ = seed_channel_with_membership(db, creator_id=author_id)
    community_id = uuid4()
    community_name = f"Hidden{uuid4().hex[:6]}"
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=f"pf-{uuid4().hex[:8]}",
            name=community_name,
            description="test",
            join_policy="closed",
            created_by=author_id,
            created_at=now,
            updated_at=now,
        )
    )
    for user_id in (author_id, member_id):
        db.execute(
            insert(scope_memberships).values(
                id=uuid4(),
                scope_kind="community",
                scope_id=community_id,
                user_id=user_id,
                role="member",
                created_at=now,
            )
        )
    secret_id, secret_slug = _project(db, author_id, "Members-only plan", now)
    _tag(db, secret_id, community_id=community_id)
    mixed_id, mixed_slug = _project(db, author_id, "Shared with a channel too", now)
    _tag(db, mixed_id, community_id=community_id)
    _tag(db, mixed_id, channel_id=channel_id)
    db.execute(
        insert(comments).values(
            id=uuid4(),
            subject_type="project",
            subject_id=secret_id,
            author_id=author_id,
            body="Private comment",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.close()

    def feed_for(viewer_id) -> list[dict]:
        headers = (
            {"Authorization": f"Bearer {create_access_token(str(viewer_id))}"} if viewer_id else {}
        )
        with TestClient(app) as client:
            response = client.get(
                f"/feeds/user/{author_name}",
                params={"sort": "recent", "limit": 100},
                headers=headers,
            )
        assert response.status_code == 200, response.text
        return response.json()["items"]

    for viewer in (outsider_id, None):
        items = feed_for(viewer)
        slugs = {item.get("slug") for item in items}
        assert secret_slug not in slugs
        assert all(item.get("body") != "Private comment" for item in items)
        mixed = next(item for item in items if item.get("slug") == mixed_slug)
        assert [tag["label"] for tag in mixed["community_tags"]] == []
        assert mixed["channel_tags"]

    items = feed_for(member_id)
    assert secret_slug in {item.get("slug") for item in items}
    assert any(item.get("body") == "Private comment" for item in items)
    mixed = next(item for item in items if item.get("slug") == mixed_slug)
    assert [tag["label"] for tag in mixed["community_tags"]] == [community_name]
