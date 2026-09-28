from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import content_votes, platform_feedback
from tests.conftest import register_and_login_client


def _auth_headers(session: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


def test_feedback_create_list_detail_and_vote(
    db_transaction: Session, isolated_client: TestClient
) -> None:
    creator = register_and_login_client(isolated_client, username="feedback-creator", ip="10.10.0.10")
    voter = register_and_login_client(isolated_client, username="feedback-voter", ip="10.10.0.11")

    create_response = isolated_client.post(
        "/feedback",
        json={
            "kind": "bug",
            "title": "Login expires in active session",
            "description": "Controls disappear until the page is reloaded.",
        },
        headers=_auth_headers(creator),
    )
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()["feedback"]
    feedback_id = created["id"]

    assert created["kind"] == "bug"
    assert created["title"] == "Login expires in active session"
    assert created["author_username"] == creator["username"]
    assert created["upvote_count"] == 0
    assert created["downvote_count"] == 0
    assert created["active_vote"] == "neutral"

    list_response = isolated_client.get("/feedback")
    assert list_response.status_code == 200, list_response.text
    items = list_response.json()["items"]
    listed = next(item for item in items if item["id"] == feedback_id)
    assert listed["title"] == created["title"]
    assert listed["description"] == created["description"]

    filtered_response = isolated_client.get("/feedback?filter=bugs&sort=trending")
    assert filtered_response.status_code == 200, filtered_response.text
    filtered = filtered_response.json()
    assert filtered["filters"]["filter"] == "bugs"
    assert filtered["filters"]["sort"] == "trending"
    assert any(item["id"] == feedback_id for item in filtered["items"])

    detail_response = isolated_client.get(f"/feedback/{feedback_id}")
    assert detail_response.status_code == 200, detail_response.text
    assert detail_response.json()["feedback"]["id"] == feedback_id

    vote_response = isolated_client.post(
        "/governance/votes",
        json={
            "target_type": "platform_feedback",
            "target_id": feedback_id,
            "direction": "up",
        },
        headers=_auth_headers(voter),
    )
    assert vote_response.status_code == 200, vote_response.text
    assert vote_response.json()["direction"] == "up"

    voted_detail = isolated_client.get(
        f"/feedback/{feedback_id}", headers=_auth_headers(voter)
    )
    assert voted_detail.status_code == 200, voted_detail.text
    voted_feedback = voted_detail.json()["feedback"]
    assert voted_feedback["upvote_count"] == 1
    assert voted_feedback["downvote_count"] == 0
    assert voted_feedback["approval_percent"] == 100.0
    assert voted_feedback["active_vote"] == "up"
    assert voted_feedback["vote_count"] == 1


def test_feedback_list_filters_suggestions(
    db_transaction: Session, isolated_client: TestClient
) -> None:
    author = register_and_login_client(isolated_client, username="feedback-author", ip="10.10.0.12")

    for kind, title in (
        ("bug", "Posting disappears from feed"),
        ("suggestion", "Save personal feed sort"),
    ):
        response = isolated_client.post(
            "/feedback",
            json={
                "kind": kind,
                "title": title,
                "description": f"Details for {title}.",
            },
            headers=_auth_headers(author),
        )
        assert response.status_code == 200, response.text

    only_suggestions = isolated_client.get("/feedback?filter=suggestions")
    assert only_suggestions.status_code == 200, only_suggestions.text
    items = only_suggestions.json()["items"]
    assert items
    assert all(item["kind"] == "suggestion" for item in items)


def test_feedback_trending_decays_stale_votes(
    db_transaction: Session, isolated_client: TestClient
) -> None:
    author = register_and_login_client(
        isolated_client, username="feedback-trend-author", ip="10.10.0.13"
    )
    voter = register_and_login_client(
        isolated_client, username="feedback-trend-voter", ip="10.10.0.14"
    )

    def create_item(title: str) -> str:
        response = isolated_client.post(
            "/feedback",
            json={"kind": "bug", "title": title, "description": f"Details for {title}."},
            headers=_auth_headers(author),
        )
        assert response.status_code == 200, response.text
        return response.json()["feedback"]["id"]

    def vote(feedback_id: str) -> None:
        response = isolated_client.post(
            "/governance/votes",
            json={"target_type": "platform_feedback", "target_id": feedback_id, "direction": "up"},
            headers=_auth_headers(voter),
        )
        assert response.status_code == 200, response.text

    stale_id = create_item("Stale bug that already got fixed")
    active_id = create_item("Old bug still getting votes")
    fresh_id = create_item("Brand new unvoted bug")

    vote(stale_id)
    vote(active_id)

    stale_time = datetime.now(UTC) - timedelta(days=10)
    db_transaction.execute(
        update(platform_feedback)
        .where(platform_feedback.c.id.in_([UUID(stale_id), UUID(active_id)]))
        .values(created_at=stale_time, updated_at=stale_time)
    )
    db_transaction.execute(
        update(content_votes)
        .where(
            content_votes.c.target_type == "platform_feedback",
            content_votes.c.target_id == UUID(stale_id),
        )
        .values(created_at=stale_time, updated_at=stale_time)
    )
    db_transaction.flush()

    items = isolated_client.get("/feedback?sort=trending").json()["items"]
    ids = [item["id"] for item in items]
    assert ids.index(active_id) < ids.index(fresh_id) < ids.index(stale_id)
