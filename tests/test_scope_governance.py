"""Channel and community settings: member votes, organizer mode, pictures."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import communities, scope_memberships
from tests.conftest import seed_user

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _users(prefix: str, count: int) -> list:
    db = SessionLocal()
    try:
        made = [seed_user(db, username_prefix=f"{prefix}{index}") for index in range(count)]
        db.commit()
        return made
    finally:
        db.close()


def _add_member(kind: str, slug: str, user_id, role: str = "member") -> None:
    db = SessionLocal()
    try:
        table = communities
        scope_id = db.execute(select(table.c.id).where(table.c.slug == slug)).scalar_one()
        db.execute(
            insert(scope_memberships).values(
                id=uuid4(),
                scope_kind=kind,
                scope_id=scope_id,
                user_id=user_id,
                role=role,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
    finally:
        db.close()


def _community(client: TestClient, owner, *, private: bool, governance: str) -> str:
    name = f"sg{uuid4().hex[:8]}"
    created = client.post(
        "/scopes/communities",
        headers=_auth(owner[0]),
        json={
            "slug": name,
            "name": name,
            "description": "seed",
            "join_policy": "closed" if private else "open",
            "governance": governance,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["community"]["slug"]


def test_members_vote_on_a_collaborative_edit(client: TestClient) -> None:
    owner, second, third, outsider = _users("sgvote", 4)
    slug = _community(client, owner, private=False, governance="collaborative")
    for member in (second, third):
        joined = client.post(f"/scopes/communities/{slug}/join", headers=_auth(member[0]))
        assert joined.status_code == 200, joined.text

    proposed = client.patch(
        f"/scopes/communities/{slug}/settings",
        headers=_auth(owner[0]),
        json={"description": "A better description"},
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()["proposals"][0]
    assert proposal["kind"] == "edit"
    assert proposed.json()["description"] == "seed"

    refused = client.post(
        f"/scopes/proposals/{proposal['id']}/vote", headers=_auth(outsider[0]), json={"vote": "yes"}
    )
    assert refused.status_code == 403, refused.text

    for member in (second, third):
        client.post(
            f"/scopes/proposals/{proposal['id']}/vote",
            headers=_auth(member[0]),
            json={"vote": "yes"},
        )
    after = client.get(f"/scopes/communities/{slug}/settings", headers=_auth(owner[0])).json()
    assert after["description"] == "A better description"
    assert after["proposals"] == []


def test_organizers_edit_directly_and_switching_needs_their_vote(client: TestClient) -> None:
    owner, helper, member = _users("sgorg", 3)
    slug = _community(client, owner, private=True, governance="organizer")
    _add_member("community", slug, helper[0])
    _add_member("community", slug, member[0])

    direct = client.patch(
        f"/scopes/communities/{slug}/settings",
        headers=_auth(owner[0]),
        json={"name": "Garden-Crew"},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["name"] == "Garden-Crew"
    assert direct.json()["governance"] == "organizer"

    blocked = client.patch(
        f"/scopes/communities/{slug}/settings", headers=_auth(member[0]), json={"name": "Mine"}
    )
    assert blocked.status_code == 403, blocked.text

    promoted = client.post(
        f"/scopes/communities/{slug}/organizers",
        headers=_auth(owner[0]),
        json={"username": helper[1], "organizer": True},
    )
    assert promoted.status_code == 200, promoted.text

    switch = client.post(
        f"/scopes/communities/{slug}/proposals",
        headers=_auth(owner[0]),
        json={"kind": "to_collaborative"},
    )
    assert switch.status_code == 200, switch.text
    assert switch.json()["governance"] == "organizer"
    proposal_id = switch.json()["proposals"][0]["id"]
    member_vote = client.post(
        f"/scopes/proposals/{proposal_id}/vote", headers=_auth(member[0]), json={"vote": "yes"}
    )
    assert member_vote.status_code == 403, member_vote.text
    client.post(
        f"/scopes/proposals/{proposal_id}/vote", headers=_auth(helper[0]), json={"vote": "yes"}
    )
    after = client.get(f"/scopes/communities/{slug}/settings", headers=_auth(owner[0])).json()
    assert after["governance"] == "collaborative"


def test_collaborative_private_community_votes_to_remove_a_member(client: TestClient) -> None:
    owner, member = _users("sgrm", 2)
    slug = _community(client, owner, private=True, governance="collaborative")
    _add_member("community", slug, member[0])
    proposed = client.post(
        f"/scopes/communities/{slug}/proposals",
        headers=_auth(owner[0]),
        json={"kind": "remove_member", "target_username": member[1]},
    )
    assert proposed.status_code == 200, proposed.text
    proposal_id = proposed.json()["proposals"][0]["id"]
    client.post(
        f"/scopes/proposals/{proposal_id}/vote", headers=_auth(member[0]), json={"vote": "yes"}
    )
    after = client.get(f"/scopes/communities/{slug}/settings", headers=_auth(owner[0])).json()
    assert after["member_count"] == 1


def test_picture_upload_and_serving(client: TestClient) -> None:
    (owner,) = _users("sgimg", 1)
    slug = _community(client, owner, private=False, governance="collaborative")
    uploaded = client.post(
        f"/scopes/communities/{slug}/image",
        headers=_auth(owner[0]),
        files={"file": ("dot.png", TINY_PNG, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    applied = client.patch(
        f"/scopes/communities/{slug}/settings",
        headers=_auth(owner[0]),
        json={"image_key": uploaded.json()["image_key"], "image_content_type": "image/png"},
    )
    assert applied.status_code == 200, applied.text
    image_url = applied.json()["image_url"]
    assert image_url
    served = client.get(image_url, headers=_auth(owner[0]))
    assert served.status_code == 200, served.text
    assert served.content == TINY_PNG
    assert served.headers["content-type"].startswith("image/png")


def test_rename_follows_the_creation_name_rules(client: TestClient) -> None:
    (owner,) = _users("sgname", 1)
    slug = _community(client, owner, private=True, governance="organizer")
    spaced = client.patch(
        f"/scopes/communities/{slug}/settings",
        headers=_auth(owner[0]),
        json={"name": "Garden Crew"},
    )
    assert spaced.status_code == 422, spaced.text
    assert "cannot contain spaces" in spaced.json()["detail"]


def test_left_rail_lists_carry_the_picture(client: TestClient) -> None:
    (owner,) = _users("sgrail", 1)
    slug = _community(client, owner, private=False, governance="collaborative")
    uploaded = client.post(
        f"/scopes/communities/{slug}/image",
        headers=_auth(owner[0]),
        files={"file": ("dot.png", TINY_PNG, "image/png")},
    )
    client.patch(
        f"/scopes/communities/{slug}/settings",
        headers=_auth(owner[0]),
        json={"image_key": uploaded.json()["image_key"], "image_content_type": "image/png"},
    )
    directory = client.get("/bootstrap", headers=_auth(owner[0])).json()["directory"]
    item = next(entry for entry in directory["communities"] if entry["slug"] == slug)
    assert item["imageUrl"].startswith(f"/scopes/communities/{slug}/image")
