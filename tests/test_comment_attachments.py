"""Photo and file attachments on project, event, and help-request comments."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import (
    blobs,
    comment_attachments,
    event_memberships,
    events,
    project_memberships,
    projects,
)
from tests.conftest import seed_user


def _seed_project(db, owner_id):
    now = datetime.now(UTC)
    project_id = uuid4()
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=f"cmt-proj-{project_id.hex[:8]}",
            title="Attachment project",
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
    return project_id


def _seed_private_event(db, owner_id):
    now = datetime.now(UTC)
    event_id = uuid4()
    db.execute(
        insert(events).values(
            id=event_id,
            slug=f"cmt-evt-{event_id.hex[:8]}",
            title="Private chat",
            description="seed",
            created_by=owner_id,
            is_private=True,
            audience="invite_only",
            current_phase_id="activity",
            time_label="Now",
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
    return event_id


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def test_project_comment_photo_is_encrypted_and_readable(client: TestClient) -> None:
    db = SessionLocal()
    try:
        owner = seed_user(db, username_prefix="cmt-photo")
        project_id = _seed_project(db, owner[0])
        db.commit()
    finally:
        db.close()

    sent = client.post(
        "/governance/comments",
        headers=_auth(owner[0]),
        data={"subject_type": "project", "subject_id": str(project_id), "body": "site photo"},
        files={"file": ("dot.png", TINY_PNG, "image/png")},
    )
    assert sent.status_code == 200, sent.text
    comment = sent.json()["comment"]
    assert comment["body"] == "site photo"
    assert comment["attachments"][0]["kind"] == "image"
    attachment_id = comment["attachments"][0]["id"]

    listed = client.get(
        "/governance/comments",
        headers=_auth(owner[0]),
        params={"subject_type": "project", "subject_id": str(project_id)},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["attachments"][0]["id"] == attachment_id

    downloaded = client.get(f"/governance/attachments/{attachment_id}", headers=_auth(owner[0]))
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == TINY_PNG

    text_only = client.post(
        "/governance/comments",
        headers=_auth(owner[0]),
        json={
            "subject_type": "project",
            "subject_id": str(project_id),
            "body": "text only",
        },
    )
    assert text_only.status_code == 200, text_only.text
    assert text_only.json()["comment"]["attachments"] == []

    db = SessionLocal()
    try:
        stored = db.execute(
            select(blobs.c.ciphertext)
            .select_from(
                blobs.join(
                    comment_attachments,
                    blobs.c.storage_key == comment_attachments.c.storage_key,
                )
            )
            .where(comment_attachments.c.id == attachment_id)
        ).scalar_one()
        assert stored != TINY_PNG
    finally:
        db.close()


def test_comment_attachment_limits_and_private_event_access(client: TestClient) -> None:
    db = SessionLocal()
    try:
        owner = seed_user(db, username_prefix="cmt-limit-a")
        outsider = seed_user(db, username_prefix="cmt-limit-b")
        event_id = _seed_private_event(db, owner[0])
        db.commit()
    finally:
        db.close()

    video = client.post(
        "/governance/comments",
        headers=_auth(owner[0]),
        data={"subject_type": "event", "subject_id": str(event_id), "body": ""},
        files={"file": ("clip.mp4", b"not-a-video", "video/mp4")},
    )
    assert video.status_code == 422, video.text

    thread = client.post(
        "/governance/comments",
        headers=_auth(owner[0]),
        data={"subject_type": "thread", "subject_id": str(uuid4()), "body": "nope"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert thread.status_code == 422, thread.text

    sent = client.post(
        "/governance/comments",
        headers=_auth(owner[0]),
        data={"subject_type": "event", "subject_id": str(event_id), "body": ""},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert sent.status_code == 200, sent.text
    attachment_id = sent.json()["comment"]["attachments"][0]["id"]
    assert sent.json()["comment"]["attachments"][0]["kind"] == "file"

    hidden = client.get(f"/governance/attachments/{attachment_id}", headers=_auth(outsider[0]))
    assert hidden.status_code == 404, hidden.text
