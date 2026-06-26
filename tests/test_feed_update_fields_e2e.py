from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db import SessionLocal
from app.main import app
from app.models import project_updates, projects, users


def test_public_feed_includes_latest_project_update_fields() -> None:
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)

    user_id = uuid4()
    project_id = uuid4()
    project_slug = f"feed-update-{str(project_id)[:8]}"
    update_body = "Shipped the first coordination milestone for this project."

    db.execute(
        insert(users).values(
            id=user_id,
            username=f"feed-update-user-{str(user_id)[:8]}",
            email=f"feed-update-{user_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Feed Update Project",
            description="seed",
            author_id=user_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="proposal",
            location_label="remote",
            is_platform_tagged=False,
            is_closed=False,
            signal_count=0,
            vote_count=0,
            comment_count=0,
            member_count=1,
            created_at=earlier,
            updated_at=earlier,
            last_activity_at=earlier,
        )
    )
    db.execute(
        insert(project_updates).values(
            id=uuid4(),
            project_id=project_id,
            title="Milestone",
            body=update_body,
            author_id=user_id,
            created_at=now,
        )
    )
    db.commit()
    db.close()

    client = TestClient(app)
    response = client.get("/feeds/public?sort=recent&limit=50&offset=0")
    assert response.status_code == 200, response.text

    item = next(item for item in response.json()["items"] if item.get("slug") == project_slug)
    assert item["latest_update_body"] == update_body
    assert item["last_update_at"] is not None
