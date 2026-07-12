"""Search must not leak closed-community-only entities."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import communities, projects, project_tags, scope_memberships, users
from app.services.search import index_document


def test_search_hides_closed_community_only_projects_from_non_members() -> None:
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    author_id = uuid4()
    outsider_id = uuid4()
    community_id = uuid4()
    project_id = uuid4()
    slug = f"private-proj-{uuid4().hex[:8]}"

    db.execute(
        insert(users).values(
            id=author_id,
            username=f"author-{author_id.hex[:8]}",
            email=f"author-{author_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=outsider_id,
            username=f"outsider-{outsider_id.hex[:8]}",
            email=f"outsider-{outsider_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=f"closed-{uuid4().hex[:8]}",
            name="Closed",
            description="test",
            join_policy="closed",
            created_by=author_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=community_id,
            user_id=author_id,
            role="member",
            created_at=now,
        )
    )
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Closed community project",
            description="hidden",
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
    db.execute(
        insert(project_tags).values(
            id=uuid4(),
            project_id=project_id,
            tag_kind="community",
            community_id=community_id,
            channel_id=None,
        )
    )
    index_document(
        db=db,
        entity_type="project",
        entity_id=project_id,
        title="Closed community project",
        summary="hidden",
        meta="Somewhere",
        href=f"/projects/{slug}",
    )
    db.commit()
    db.close()

    outsider_token = create_access_token(str(outsider_id))

    with TestClient(app) as client:
        response = client.get(
            "/search",
            params={"q": slug},
            headers={"Authorization": f"Bearer {outsider_token}"},
        )

    assert response.status_code == 200
    items = response.json().get("items", [])
    assert not any(item.get("href") == f"/projects/{slug}" for item in items)
