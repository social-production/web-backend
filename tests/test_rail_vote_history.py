"""Cast votes of every rail kind belong in activity-rail history."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import (
    event_memberships,
    event_update_request_votes,
    event_update_requests,
    events,
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_update_request_votes,
    project_update_requests,
    projects,
    users,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user(db: Session, now: datetime) -> tuple[UUID, str]:
    user_id = uuid4()
    username = f"railhist-{user_id.hex[:8]}"
    db.execute(
        insert(users).values(
            id=user_id,
            username=username,
            email=f"{username}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    return user_id, username


def _seed_project(db: Session, now: datetime, owner_id: UUID) -> tuple[UUID, str]:
    project_id = uuid4()
    slug = f"railhist-proj-{project_id.hex[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Rail history project",
            description="seed",
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
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=owner_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    return project_id, slug


def _seed_event(db: Session, now: datetime, owner_id: UUID) -> tuple[UUID, str]:
    event_id = uuid4()
    slug = f"railhist-evt-{event_id.hex[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=slug,
            title="Rail history event",
            description="seed",
            created_by=owner_id,
            is_private=False,
            current_phase_id="proposal",
            time_label="Soon",
            location_label="Workshop",
            scheduled_at=now,
            vote_count=0,
            comment_count=0,
            going_count=0,
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(event_memberships).values(
            event_id=event_id,
            user_id=owner_id,
            role="member",
            joined_at=now,
        )
    )
    return event_id, slug


def test_bootstrap_history_includes_update_and_edit_votes() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, project_slug = _seed_project(db, now, owner_id)
    event_id, event_slug = _seed_event(db, now, owner_id)

    update_id = uuid4()
    edit_id = uuid4()
    event_update_id = uuid4()
    db.execute(
        insert(project_update_requests).values(
            id=update_id,
            project_id=project_id,
            body="Shipped the workshop notes for everyone.",
            author_id=owner_id,
            status="open",
            created_at=now,
        )
    )
    db.execute(
        insert(project_update_request_votes).values(
            request_id=update_id,
            voter_id=owner_id,
            vote="yes",
            created_at=now,
        )
    )
    db.execute(
        insert(project_edit_requests).values(
            id=edit_id,
            project_id=project_id,
            title="Rename the workshop",
            description="Clearer title",
            author_id=owner_id,
            status="open",
            created_at=now,
        )
    )
    db.execute(
        insert(project_edit_request_votes).values(
            request_id=edit_id,
            voter_id=owner_id,
            vote="no",
            created_at=now,
        )
    )
    db.execute(
        insert(event_update_requests).values(
            id=event_update_id,
            event_id=event_id,
            body="Doors open at six.",
            author_id=owner_id,
            status="open",
            created_at=now,
        )
    )
    db.execute(
        insert(event_update_request_votes).values(
            request_id=event_update_id,
            voter_id=owner_id,
            vote="yes",
            created_at=now,
        )
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.get("/bootstrap", headers=_auth_header(token))

    assert response.status_code == 200, response.text
    history = response.json()["activityRailHistory"]
    vote_hrefs = {item["href"] for item in history if item["kind"] == "vote"}
    vote_metas = {item["meta"] for item in history if item["kind"] == "vote"}

    assert (
        f"/projects/{project_slug}?open=vote&voteKind=update&voteTarget={update_id}" in vote_hrefs
    )
    assert f"/projects/{project_slug}?open=vote&voteKind=edit&voteTarget={edit_id}" in vote_hrefs
    assert (
        f"/events/{event_slug}?open=vote&voteKind=update&voteTarget={event_update_id}" in vote_hrefs
    )
    assert any(meta.startswith("You voted Yes") for meta in vote_metas)
    assert any(meta.startswith("You voted No") for meta in vote_metas)
    for item in history:
        if item["kind"] == "vote":
            assert item["viewerParticipated"] is True
            assert item["outcome"] in {"passed", "failed", "open"}
