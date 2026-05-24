"""Automated E2E test for the feeds API."""
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import (
    channels,
    event_tags,
    events,
    project_tags,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    users,
)


def run():
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    uid = uuid4()
    db.execute(insert(users).values(
        id=uid,
        username=f"feeduser-{str(uid)[:8]}",
        email=f"feed-{uid}@test.invalid",
        password_hash="x",
        created_at=now,
        updated_at=now,
    ))

    cid = uuid4()
    db.execute(insert(channels).values(
        id=cid,
        slug=f"feed-ch-{cid}",
        name="Feed Channel",
        description="test",
        created_by=uid,
        created_at=now,
        updated_at=now,
    ))

    db.execute(insert(scope_memberships).values(
        id=uuid4(),
        scope_kind="channel",
        scope_id=cid,
        user_id=uid,
        role="member",
        created_at=now,
    ))

    pid = uuid4()
    db.execute(insert(projects).values(
        id=pid,
        slug=f"feed-proj-{pid}",
        title="Feed Project Alpha",
        description="desc",
        author_id=uid,
        project_mode="collaborative",
        current_phase_id="phase-1",
        stage_label="early",
        location_label="remote",
        signal_count=5,
        vote_count=3,
        comment_count=2,
        member_count=1,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    ))
    db.execute(insert(project_tags).values(
        id=uuid4(),
        project_id=pid,
        tag_kind="channel",
        channel_id=cid,
        community_id=None,
    ))

    tid = uuid4()
    db.execute(insert(threads).values(
        id=tid,
        slug=f"feed-thread-{tid}",
        title="Feed Thread Beta",
        body="body text",
        author_id=uid,
        vote_count=7,
        comment_count=1,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    ))
    db.execute(insert(thread_tags).values(
        id=uuid4(),
        thread_id=tid,
        tag_kind="channel",
        channel_id=cid,
        community_id=None,
    ))

    eid = uuid4()
    db.execute(insert(events).values(
        id=eid,
        slug=f"feed-event-{eid}",
        title="Feed Event Gamma",
        description="desc",
        created_by=uid,
        is_private=False,
        current_phase_id="phase-1",
        time_label="TBD",
        location_label="online",
        vote_count=2,
        comment_count=0,
        going_count=4,
        member_count=3,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    ))
    db.execute(insert(event_tags).values(
        id=uuid4(),
        event_id=eid,
        tag_kind="channel",
        channel_id=cid,
        community_id=None,
    ))

    db.commit()
    db.close()

    token = create_access_token(subject=str(uid))
    print(json.dumps({
        "token": token,
        "pid": str(pid),
        "tid": str(tid),
        "eid": str(eid),
    }))


if __name__ == "__main__":
    run()
