"""Closed-community member list access control."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import communities, scope_memberships, users


def _seed_closed_community_with_member() -> tuple[str, str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)
    member_id = uuid4()
    outsider_id = uuid4()
    community_id = uuid4()
    slug = f"closed-{uuid4().hex[:8]}"

    db.execute(
        insert(users).values(
            id=member_id,
            username=f"member-{member_id.hex[:8]}",
            email=f"member-{member_id}@test.invalid",
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
            slug=slug,
            name="Closed test community",
            description="test",
            join_policy="closed",
            created_by=member_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="community",
            scope_id=community_id,
            user_id=member_id,
            role="member",
            created_at=now,
        )
    )
    db.commit()
    db.close()

    member_token = create_access_token(str(member_id))
    outsider_token = create_access_token(str(outsider_id))
    return slug, member_token, outsider_token


def test_closed_community_members_hidden_from_non_members() -> None:
    slug, member_token, outsider_token = _seed_closed_community_with_member()

    with TestClient(app) as client:
        outsider_response = client.get(
            f"/scopes/communities/{slug}/members",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        member_response = client.get(
            f"/scopes/communities/{slug}/members",
            headers={"Authorization": f"Bearer {member_token}"},
        )

    assert outsider_response.status_code == 404
    assert member_response.status_code == 200
    assert len(member_response.json()["items"]) >= 1
