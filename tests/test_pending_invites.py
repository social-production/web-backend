"""Direct invites to a private community wait for the person to accept or decline."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import communities, scope_memberships
from tests.conftest import seed_user


def _setup():
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = seed_user(db, username_prefix="inv-owner")
    guest_id, guest_name = seed_user(db, username_prefix="inv-guest")
    community_id = uuid4()
    slug = f"inv-{uuid4().hex[:8]}"
    db.execute(
        insert(communities).values(
            id=community_id,
            slug=slug,
            name="Invite test",
            description="test",
            join_policy="closed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=community_id,
            user_id=owner_id,
            role="manager",
            created_at=now,
        )
    )
    db.commit()
    db.close()
    owner = {"Authorization": f"Bearer {create_access_token(str(owner_id))}"}
    guest = {"Authorization": f"Bearer {create_access_token(str(guest_id))}"}
    return owner, guest, guest_id, guest_name, community_id, slug


def _is_member(community_id, user_id) -> bool:
    db = SessionLocal()
    try:
        return (
            db.execute(
                select(scope_memberships.c.user_id).where(
                    scope_memberships.c.scope_id == community_id,
                    scope_memberships.c.user_id == user_id,
                )
            ).first()
            is not None
        )
    finally:
        db.close()


def test_invitee_joins_only_after_accepting() -> None:
    owner, guest, guest_id, guest_name, community_id, slug = _setup()
    with TestClient(app) as client:
        sent = client.post(
            f"/scopes/communities/{slug}/invite-user", headers=owner, json={"username": guest_name}
        )
        assert sent.status_code == 200, sent.text
        assert not _is_member(community_id, guest_id)

        pending = client.get("/invites", headers=guest).json()["items"]
        assert [item["title"] for item in pending] == ["Invite test"]

        accepted = client.post(f"/invites/{pending[0]['id']}/accept", headers=guest)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["href"] == f"/communities/{slug}"
        assert _is_member(community_id, guest_id)
        assert client.get("/invites", headers=guest).json()["items"] == []


def test_declining_leaves_them_out() -> None:
    owner, guest, guest_id, guest_name, community_id, slug = _setup()
    with TestClient(app) as client:
        client.post(
            f"/scopes/communities/{slug}/invite-user", headers=owner, json={"username": guest_name}
        )
        pending = client.get("/invites", headers=guest).json()["items"]
        declined = client.post(f"/invites/{pending[0]['id']}/decline", headers=guest)
        assert declined.status_code == 200, declined.text
        assert not _is_member(community_id, guest_id)
        again = client.post(f"/invites/{pending[0]['id']}/accept", headers=guest)
        assert again.status_code == 409
