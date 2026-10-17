"""Private-community items show in the public feed only for that community's members."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import communities, project_tags, projects, scope_memberships
from tests.conftest import seed_user


def test_public_feed_shows_private_community_items_to_members_only() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    member_id, _ = seed_user(db, username_prefix="pc-member")
    outsider_id, _ = seed_user(db, username_prefix="pc-outsider")
    community_id = uuid4()
    project_id = uuid4()
    slug = f"pc-proj-{uuid4().hex[:8]}"
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=f"pc-{uuid4().hex[:8]}",
            name="Closed",
            description="test",
            join_policy="closed",
            created_by=member_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=community_id,
            user_id=member_id,
            role="member",
            created_at=now,
        )
    )
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Members-only garden",
            description="hidden from outsiders",
            author_id=member_id,
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
    db.execute(
        insert(project_tags).values(
            id=uuid4(),
            project_id=project_id,
            tag_kind="community",
            community_id=community_id,
            channel_id=None,
        )
    )
    db.commit()
    db.close()

    def slugs_for(user_id) -> list[str]:
        with TestClient(app) as client:
            response = client.get(
                "/feeds/public",
                params={"sort": "recent", "limit": 100},
                headers={"Authorization": f"Bearer {create_access_token(str(user_id))}"},
            )
        assert response.status_code == 200, response.text
        return [item.get("slug") for item in response.json().get("items", [])]

    assert slug in slugs_for(member_id)
    assert slug not in slugs_for(outsider_id)
