from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models import communities
from app.services.scopes import list_discoverable_scopes
from tests.conftest import seed_channel_with_membership, seed_scope_membership, seed_user


def _seed_community(db: Session, *, creator_id: UUID, join_policy: str) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    community_id = uuid4()
    slug = f"cm-{str(community_id)[:8]}"
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=slug,
            name=f"Community {slug}",
            description="seed",
            join_policy=join_policy,
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
    )
    seed_scope_membership(db, scope_kind="community", scope_id=community_id, user_id=creator_id)
    return community_id, slug


def test_discover_channels_orders_by_member_count(db_transaction: Session) -> None:
    owner_id, _ = seed_user(db_transaction, username_prefix="disc-owner")
    viewer_id, _ = seed_user(db_transaction, username_prefix="disc-viewer")
    extra_id, _ = seed_user(db_transaction, username_prefix="disc-extra")
    _, small_slug = seed_channel_with_membership(
        db_transaction, creator_id=owner_id, channel_name="Aaa small"
    )
    big_id, big_slug = seed_channel_with_membership(
        db_transaction, creator_id=owner_id, channel_name="Zzz big"
    )
    seed_scope_membership(db_transaction, scope_kind="channel", scope_id=big_id, user_id=viewer_id)
    seed_scope_membership(db_transaction, scope_kind="channel", scope_id=big_id, user_id=extra_id)
    db_transaction.flush()

    result = list_discoverable_scopes(db_transaction, viewer_id, "channel", limit=500)
    slugs = [item["slug"] for item in result["items"]]

    assert "platform" not in slugs
    assert "stewardship" not in slugs
    assert slugs.index(big_slug) < slugs.index(small_slug)
    by_slug = {item["slug"]: item for item in result["items"]}
    assert by_slug[big_slug]["member_count"] == 3
    assert by_slug[big_slug]["viewer_is_member"] is True
    assert by_slug[small_slug]["member_count"] == 1
    assert by_slug[small_slug]["viewer_is_member"] is False
    counts = [item["member_count"] for item in result["items"]]
    assert counts == sorted(counts, reverse=True)


def test_discover_communities_hides_closed_from_non_members(db_transaction: Session) -> None:
    owner_id, _ = seed_user(db_transaction, username_prefix="disc-cowner")
    stranger_id, _ = seed_user(db_transaction, username_prefix="disc-stranger")
    _, open_slug = _seed_community(db_transaction, creator_id=owner_id, join_policy="open")
    _, closed_slug = _seed_community(db_transaction, creator_id=owner_id, join_policy="closed")
    db_transaction.flush()

    stranger = list_discoverable_scopes(db_transaction, stranger_id, "community", limit=500)
    stranger_slugs = {item["slug"] for item in stranger["items"]}
    assert open_slug in stranger_slugs
    assert closed_slug not in stranger_slugs

    guest = list_discoverable_scopes(db_transaction, None, "community", limit=500)
    guest_slugs = {item["slug"] for item in guest["items"]}
    assert open_slug in guest_slugs
    assert closed_slug not in guest_slugs

    member = list_discoverable_scopes(db_transaction, owner_id, "community", limit=500)
    member_items = {item["slug"]: item for item in member["items"]}
    assert member_items[closed_slug]["visibility"] == "private"
    assert member_items[closed_slug]["viewer_is_member"] is True


def test_discover_endpoint_allows_guests(
    db_transaction: Session, isolated_client: TestClient
) -> None:
    owner_id, _ = seed_user(db_transaction, username_prefix="disc-guest")
    _, slug = seed_channel_with_membership(db_transaction, creator_id=owner_id)
    db_transaction.flush()

    response = isolated_client.get("/scopes/discover?kind=channel&limit=500")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "channel"
    item = next(item for item in body["items"] if item["slug"] == slug)
    assert item["href"] == f"/channels/{slug}"
    assert item["viewer_is_member"] is False

    bad = isolated_client.get("/scopes/discover?kind=nope")
    assert bad.status_code == 422
