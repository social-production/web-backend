"""Phase 3 feed engagement: project/event signals, feed counts, and server-owned trending."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import (
    channels,
    event_signals,
    event_tags,
    events,
    help_request_tags,
    help_requests,
    project_signals,
    project_tags,
    projects,
    thread_tags,
    threads,
    users,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user(db: Session, now: datetime, *, prefix: str = "feed-eng") -> tuple[UUID, str]:
    user_id = uuid4()
    username = f"{prefix}-{user_id.hex[:8]}"
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


def _seed_channel(db: Session, *, owner_id: UUID, now: datetime) -> tuple[UUID, str]:
    channel_id = uuid4()
    slug = f"feed-eng-ch-{channel_id.hex[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=slug,
            name="Feed Engagement Test Channel",
            description="seed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )
    return channel_id, slug


def _seed_project(
    db: Session,
    *,
    owner_id: UUID,
    now: datetime,
    channel_id: UUID | None = None,
    title: str = "Signal Project",
    is_closed: bool = False,
    signal_count: int = 0,
    vote_count: int = 0,
    comment_count: int = 0,
    member_count: int = 0,
    last_activity_at: datetime | None = None,
) -> tuple[UUID, str]:
    project_id = uuid4()
    slug = f"sig-proj-{project_id.hex[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title=title,
            description="seed project",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="early",
            location_label="online",
            is_platform_tagged=False,
            is_closed=is_closed,
            signal_count=signal_count,
            vote_count=vote_count,
            comment_count=comment_count,
            member_count=member_count,
            created_at=now,
            updated_at=now,
            last_activity_at=last_activity_at or now,
        )
    )
    if channel_id is not None:
        db.execute(
            insert(project_tags).values(
                id=uuid4(),
                project_id=project_id,
                tag_kind="channel",
                channel_id=channel_id,
                community_id=None,
            )
        )
    return project_id, slug


def _seed_event(
    db: Session,
    *,
    owner_id: UUID,
    now: datetime,
    channel_id: UUID | None = None,
    title: str = "Signal Event",
    current_phase_id: str = "proposal",
    vote_count: int = 0,
    comment_count: int = 0,
    member_count: int = 0,
    going_count: int = 0,
    last_activity_at: datetime | None = None,
) -> tuple[UUID, str]:
    event_id = uuid4()
    slug = f"sig-evt-{event_id.hex[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=slug,
            title=title,
            description="seed event",
            created_by=owner_id,
            is_private=False,
            current_phase_id=current_phase_id,
            time_label="TBD",
            location_label="online",
            vote_count=vote_count,
            comment_count=comment_count,
            going_count=going_count,
            member_count=member_count,
            created_at=now,
            updated_at=now,
            last_activity_at=last_activity_at or now,
        )
    )
    if channel_id is not None:
        db.execute(
            insert(event_tags).values(
                id=uuid4(),
                event_id=event_id,
                tag_kind="channel",
                channel_id=channel_id,
                community_id=None,
            )
        )
    return event_id, slug


def _seed_thread(
    db: Session,
    *,
    owner_id: UUID,
    now: datetime,
    channel_id: UUID | None = None,
    title: str = "Signal Thread",
    vote_count: int = 0,
    comment_count: int = 0,
    last_activity_at: datetime | None = None,
) -> tuple[UUID, str]:
    thread_id = uuid4()
    slug = f"sig-thread-{thread_id.hex[:8]}"
    db.execute(
        insert(threads).values(
            id=thread_id,
            slug=slug,
            title=title,
            body="body text",
            author_id=owner_id,
            vote_count=vote_count,
            comment_count=comment_count,
            created_at=now,
            updated_at=now,
            last_activity_at=last_activity_at or now,
        )
    )
    if channel_id is not None:
        db.execute(
            insert(thread_tags).values(
                id=uuid4(),
                thread_id=thread_id,
                tag_kind="channel",
                channel_id=channel_id,
                community_id=None,
            )
        )
    return thread_id, slug


def _seed_help_request(
    db: Session,
    *,
    owner_id: UUID,
    now: datetime,
    channel_id: UUID | None = None,
    title: str = "Signal Help Request",
) -> UUID:
    help_request_id = uuid4()
    db.execute(
        insert(help_requests).values(
            id=help_request_id,
            author_id=owner_id,
            title=title,
            body="need help",
            location_label="online",
            schedule_label="flexible",
            needed_at=now,
            vote_count=0,
            comment_count=0,
            created_at=now,
        )
    )
    if channel_id is not None:
        db.execute(
            insert(help_request_tags).values(
                id=uuid4(),
                help_request_id=help_request_id,
                tag_kind="channel",
                channel_id=channel_id,
                community_id=None,
            )
        )
    return help_request_id


def _add_project_signal(
    db: Session, *, project_id: UUID, user_id: UUID, signal_type: str, now: datetime
) -> None:
    db.execute(
        insert(project_signals).values(
            id=uuid4(),
            project_id=project_id,
            user_id=user_id,
            signal_type=signal_type,
            created_at=now,
        )
    )


def _add_event_signal(
    db: Session, *, event_id: UUID, user_id: UUID, signal_type: str, now: datetime
) -> None:
    db.execute(
        insert(event_signals).values(
            id=uuid4(),
            event_id=event_id,
            user_id=user_id,
            signal_type=signal_type,
            created_at=now,
        )
    )


def _find_item(items: list[dict[str, object]], entity_id: UUID) -> dict[str, object]:
    return next(item for item in items if item["id"] == str(entity_id))


def _get_scope_feed(client: TestClient, channel_slug: str, **params: object) -> dict[str, object]:
    query = {"kind": "channel", "slug": channel_slug, "limit": 50, **params}
    response = client.get("/feeds/scope", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _get_user_feed(client: TestClient, username: str, **params: object) -> dict[str, object]:
    response = client.get(f"/feeds/user/{username}", params={"limit": 50, **params})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Governance: votes on projects/events are rejected.
# ---------------------------------------------------------------------------


def test_project_vote_rejected_via_governance() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            "/governance/votes",
            headers=_auth_header(token),
            json={"target_type": "project", "target_id": str(uuid4()), "direction": "up"},
        )
    assert response.status_code == 422


def test_event_vote_rejected_via_governance() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            "/governance/votes",
            headers=_auth_header(token),
            json={"target_type": "event", "target_id": str(uuid4()), "direction": "up"},
        )
    assert response.status_code == 422


def test_thread_vote_still_accepted_via_governance() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    thread_id, _ = _seed_thread(db, owner_id=owner_id, now=now)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            "/governance/votes",
            headers=_auth_header(token),
            json={"target_type": "thread", "target_id": str(thread_id), "direction": "up"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Closed projects/events reject new signals.
# ---------------------------------------------------------------------------


def test_closed_project_rejects_signal() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _project_id, slug = _seed_project(db, owner_id=owner_id, now=now, is_closed=True)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )
    assert response.status_code == 409


def test_open_project_accepts_signal() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _project_id, slug = _seed_project(db, owner_id=owner_id, now=now, is_closed=False)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )
    assert response.status_code == 200


def test_project_signal_toggle_succeeds_when_cache_write_fails() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _project_id, slug = _seed_project(db, owner_id=owner_id, now=now, is_closed=False)
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with (
        patch(
            "app.services.projects.actions._write_signal_counts_cache",
            new_callable=AsyncMock,
            side_effect=ConnectionError("redis down"),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            f"/projects/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "added"
    assert body["signal_type"] == "demand"
    assert body["signals"] == {"demand": 1, "opposition": 0, "total": 1}


def test_event_signal_toggle_succeeds_when_cache_write_fails() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _event_id, slug = _seed_event(db, owner_id=owner_id, now=now, current_phase_id="proposal")
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with (
        patch(
            "app.services.events.actions._write_signal_counts_cache",
            new_callable=AsyncMock,
            side_effect=ConnectionError("redis down"),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            f"/events/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "added"
    assert body["signal_type"] == "demand"
    assert body["signals"] == {"demand": 1, "opposition": 0, "total": 1}


def test_closed_event_rejects_signal() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _event_id, slug = _seed_event(db, owner_id=owner_id, now=now, current_phase_id="closed")
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/events/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )
    assert response.status_code == 409


def test_open_event_accepts_signal() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    _event_id, slug = _seed_event(db, owner_id=owner_id, now=now, current_phase_id="proposal")
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/events/{slug}/signals",
            headers=_auth_header(token),
            json={"signal_type": "demand"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Feed items expose support/oppose counts, favorability, and viewer_signal.
# ---------------------------------------------------------------------------


def test_project_signal_counts_and_favorability_on_feed() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)
    supporter_id, _ = _seed_user(db, now)
    opposer_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, owner_id=owner_id, now=now, channel_id=channel_id)
    db.commit()
    db.close()

    supporter_token = create_access_token(str(supporter_id))
    opposer_token = create_access_token(str(opposer_id))
    with TestClient(app) as client:
        # Use the real toggle endpoint so the denormalized `signal_count` on the
        # project row stays in sync with the underlying project_signals rows.
        support_response = client.post(
            f"/projects/{slug}/signals",
            headers=_auth_header(supporter_token),
            json={"signal_type": "demand"},
        )
        oppose_response = client.post(
            f"/projects/{slug}/signals",
            headers=_auth_header(opposer_token),
            json={"signal_type": "opposition"},
        )
        assert support_response.status_code == 200
        assert oppose_response.status_code == 200

        response = client.get(
            "/feeds/scope",
            params={"kind": "channel", "slug": channel_slug, "sort": "recent", "limit": 50},
            headers=_auth_header(supporter_token),
        )
    assert response.status_code == 200
    body = response.json()
    item = _find_item(body["items"], project_id)
    assert item["support_count"] == 1
    assert item["oppose_count"] == 1
    assert item["signal_count"] == 2
    assert item["favorability"] == 0.5
    assert item["viewer_signal"] == "demand"
    assert item["active_vote"] == 0


def test_project_with_no_signals_has_null_favorability() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)
    project_id, _slug = _seed_project(db, owner_id=owner_id, now=now, channel_id=channel_id)
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="recent")
    item = _find_item(body["items"], project_id)
    assert item["support_count"] == 0
    assert item["oppose_count"] == 0
    assert item["favorability"] is None
    assert item["viewer_signal"] is None


def test_event_signal_counts_on_feed() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)
    supporter_id, _ = _seed_user(db, now)
    event_id, _slug = _seed_event(db, owner_id=owner_id, now=now, channel_id=channel_id)
    _add_event_signal(db, event_id=event_id, user_id=supporter_id, signal_type="demand", now=now)
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="recent")
    item = _find_item(body["items"], event_id)
    assert item["support_count"] == 1
    assert item["oppose_count"] == 0
    assert item["favorability"] == 1.0


# ---------------------------------------------------------------------------
# Server-owned trending: type-aware score ordering.
# ---------------------------------------------------------------------------


def test_trending_order_is_type_aware_and_deterministic() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    # Highest score: thread with a big, capped vote_count and comment_count.
    high_thread_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        vote_count=1000,
        comment_count=1000,
        last_activity_at=now,
    )
    # Medium score: project with strong signals and members.
    mid_project_id, _mid_slug = _seed_project(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        comment_count=5,
        member_count=5,
        last_activity_at=now,
    )
    for _ in range(10):
        extra_user_id, _ = _seed_user(db, now)
        _add_project_signal(
            db, project_id=mid_project_id, user_id=extra_user_id, signal_type="demand", now=now
        )
    # Lowest score: thread with nothing going for it.
    low_thread_id, _ = _seed_thread(
        db, owner_id=owner_id, now=now, channel_id=channel_id, last_activity_at=now
    )

    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="trending", filter="all")
    assert body["sort"] == "trending"
    ids = [item["id"] for item in body["items"]]

    high_index = ids.index(str(high_thread_id))
    mid_index = ids.index(str(mid_project_id))
    low_index = ids.index(str(low_thread_id))
    assert high_index < mid_index < low_index


def test_recent_sort_orders_by_last_activity() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    older_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Older",
        last_activity_at=now - timedelta(hours=5),
    )
    newer_id, _ = _seed_thread(
        db, owner_id=owner_id, now=now, channel_id=channel_id, title="Newer", last_activity_at=now
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="recent")
    ids = [item["id"] for item in body["items"]]
    assert ids.index(str(newer_id)) < ids.index(str(older_id))


def test_oldest_sort_orders_by_last_activity_ascending() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    older_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Older",
        last_activity_at=now - timedelta(hours=5),
    )
    newer_id, _ = _seed_thread(
        db, owner_id=owner_id, now=now, channel_id=channel_id, title="Newer", last_activity_at=now
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="oldest")
    assert body["sort"] == "oldest"
    ids = [item["id"] for item in body["items"]]
    assert ids.index(str(older_id)) < ids.index(str(newer_id))


def test_top_sort_orders_by_raw_vote_count_including_negatives() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    low_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Downvoted",
        last_activity_at=now,
        vote_count=-8,
    )
    high_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Upvoted",
        last_activity_at=now - timedelta(hours=5),
        vote_count=12,
    )
    mid_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Neutral",
        last_activity_at=now - timedelta(hours=1),
        vote_count=0,
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="top")
    assert body["sort"] == "top"
    ids = [item["id"] for item in body["items"]]
    assert ids.index(str(high_id)) < ids.index(str(mid_id)) < ids.index(str(low_id))


def test_user_feed_top_sort_orders_by_raw_vote_count_including_negatives() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, username = _seed_user(db, now, prefix="top-user")

    low_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        title="Downvoted",
        last_activity_at=now,
        vote_count=-8,
    )
    high_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        title="Upvoted",
        last_activity_at=now - timedelta(hours=5),
        vote_count=12,
    )
    mid_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        title="Neutral",
        last_activity_at=now - timedelta(hours=1),
        vote_count=0,
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_user_feed(client, username, sort="top")
    assert body["sort"] == "top"
    ids = [item["id"] for item in body["items"]]
    assert ids.index(str(high_id)) < ids.index(str(mid_id)) < ids.index(str(low_id))


def test_popular_alias_normalizes_to_trending() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)
    _seed_thread(db, owner_id=owner_id, now=now, channel_id=channel_id)
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="popular")
    assert body["sort"] == "trending"


# ---------------------------------------------------------------------------
# Window filtering.
# ---------------------------------------------------------------------------


def test_window_filter_excludes_stale_items() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    recent_id, _ = _seed_thread(
        db, owner_id=owner_id, now=now, channel_id=channel_id, title="Recent", last_activity_at=now
    )
    stale_id, _ = _seed_thread(
        db,
        owner_id=owner_id,
        now=now,
        channel_id=channel_id,
        title="Stale",
        last_activity_at=now - timedelta(days=60),
    )
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, sort="recent", window="month")
    assert body["window"] == "month"
    ids = {item["id"] for item in body["items"]}
    assert str(recent_id) in ids
    assert str(stale_id) not in ids


# ---------------------------------------------------------------------------
# Entity filter.
# ---------------------------------------------------------------------------


def test_help_requests_filter_returns_only_help_requests() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    project_id, _slug = _seed_project(db, owner_id=owner_id, now=now, channel_id=channel_id)
    thread_id, _ = _seed_thread(db, owner_id=owner_id, now=now, channel_id=channel_id)
    help_request_id = _seed_help_request(db, owner_id=owner_id, now=now, channel_id=channel_id)
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, filter="help_requests")
    assert body["filter"] == "help_requests"
    entity_types = {item["entity_type"] for item in body["items"]}
    assert entity_types <= {"help_request"}
    ids = {item["id"] for item in body["items"]}
    assert str(help_request_id) in ids
    assert str(project_id) not in ids
    assert str(thread_id) not in ids


def test_projects_filter_excludes_other_entities() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    project_id, _slug = _seed_project(db, owner_id=owner_id, now=now, channel_id=channel_id)
    thread_id, _ = _seed_thread(db, owner_id=owner_id, now=now, channel_id=channel_id)
    db.commit()
    db.close()

    with TestClient(app) as client:
        body = _get_scope_feed(client, channel_slug, filter="projects")
    ids = {item["id"] for item in body["items"]}
    assert str(project_id) in ids
    assert str(thread_id) not in ids


# ---------------------------------------------------------------------------
# Pagination stability smoke test.
# ---------------------------------------------------------------------------


def test_pagination_is_stable_across_pages() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    channel_id, channel_slug = _seed_channel(db, owner_id=owner_id, now=now)

    thread_ids = []
    for i in range(6):
        thread_id, _ = _seed_thread(
            db,
            owner_id=owner_id,
            now=now,
            channel_id=channel_id,
            title=f"Pagination thread {i}",
            last_activity_at=now - timedelta(minutes=i),
        )
        thread_ids.append(thread_id)
    db.commit()
    db.close()

    with TestClient(app) as client:
        page_1 = client.get(
            "/feeds/scope",
            params={
                "kind": "channel",
                "slug": channel_slug,
                "sort": "recent",
                "filter": "threads",
                "limit": 3,
                "offset": 0,
            },
        )
        page_2 = client.get(
            "/feeds/scope",
            params={
                "kind": "channel",
                "slug": channel_slug,
                "sort": "recent",
                "filter": "threads",
                "limit": 3,
                "offset": 3,
            },
        )
    assert page_1.status_code == 200
    assert page_2.status_code == 200

    page_1_ids = [item["id"] for item in page_1.json()["items"]]
    page_2_ids = [item["id"] for item in page_2.json()["items"]]

    assert len(page_1_ids) == 3
    assert len(page_2_ids) == 3
    assert set(page_1_ids).isdisjoint(page_2_ids)

    combined = page_1_ids + page_2_ids
    expected_order = [str(tid) for tid in thread_ids]
    assert combined == expected_order
