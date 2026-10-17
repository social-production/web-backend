"""Passed edits change only their own fields, so they never undo each other."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import project_memberships, projects
from tests.conftest import seed_user


def _seed_project() -> tuple[dict[str, str], str, dict[str, str]]:
    db = SessionLocal()
    try:
        owner_id, _ = seed_user(db, username_prefix="partial")
        member_id, _ = seed_user(db, username_prefix="partial-m")
        now = datetime.now(UTC)
        project_id = uuid4()
        slug = f"partial-{project_id.hex[:8]}"
        db.execute(
            insert(projects).values(
                id=project_id,
                slug=slug,
                title="Repair night",
                description="Fix things together.",
                author_id=owner_id,
                project_mode="productive",
                project_subtype="standard",
                current_phase_id="phase-2",
                stage_label="planning",
                location_label="online",
                is_platform_tagged=False,
                is_closed=False,
                signal_count=0,
                vote_count=0,
                comment_count=0,
                member_count=1,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
        for user_id in (owner_id, member_id):
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
        return (
            {"Authorization": f"Bearer {create_access_token(str(owner_id))}"},
            slug,
            {"Authorization": f"Bearer {create_access_token(str(member_id))}"},
        )
    finally:
        db.close()


def _propose(client: TestClient, headers, slug: str, title: str, description: str) -> dict:
    created = client.post(
        f"/projects/{slug}/edit-requests",
        headers=headers,
        json={"title": title, "description": description},
    )
    assert created.status_code == 200, created.text
    return created.json()["request"]


def _pass(client: TestClient, headers, slug: str, request_id: str) -> None:
    voted = client.post(
        f"/projects/{slug}/edit-requests/{request_id}/vote",
        headers=headers,
        json={"vote": "yes"},
    )
    assert voted.status_code == 200, voted.text
    assert voted.json()["executed"] is True


def test_two_open_edits_both_stick(client: TestClient, monkeypatch) -> None:
    import app.services.projects.phases.edit_requests as edit_requests

    # Two active members: the proposer's Yes is counted, but the edit needs the other one too.
    monkeypatch.setattr(edit_requests, "_project_vote_population", lambda _db, _row: 2)
    headers, slug, member = _seed_project()
    # Both are drafted from the same page while neither has passed yet.
    title_edit = _propose(client, headers, slug, "Repair night (Town Hall)", "Fix things together.")
    description_edit = _propose(client, headers, slug, "Repair night", "Bring broken toasters.")
    assert title_edit["description"] is None
    assert description_edit["title"] is None
    # The proposer's own Yes is counted when the vote opens.
    assert title_edit["vote_summary"]["yes_count"] == 1

    _pass(client, member, slug, title_edit["id"])
    _pass(client, member, slug, description_edit["id"])

    db = SessionLocal()
    try:
        row = db.execute(select(projects).where(projects.c.slug == slug)).mappings().one()
    finally:
        db.close()
    assert row["title"] == "Repair night (Town Hall)"
    assert row["description"] == "Bring broken toasters."


def test_an_edit_that_changes_nothing_is_refused(client: TestClient) -> None:
    headers, slug, _member = _seed_project()
    unchanged = client.post(
        f"/projects/{slug}/edit-requests",
        headers=headers,
        json={"title": "Repair night", "description": "Fix things together."},
    )
    assert unchanged.status_code == 422, unchanged.text
