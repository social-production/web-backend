"""Tests for project close + conversion transactions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import (
    channels,
    project_conversions,
    project_inherited_decisions,
    project_links,
    project_memberships,
    project_tags,
    projects,
    users,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_project(*, mode: str = "productive", subtype: str = "standard") -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id = uuid4()
    member_id = uuid4()
    owner_name = f"cvt-owner-{str(owner_id)[:8]}"
    member_name = f"cvt-member-{str(member_id)[:8]}"

    for user_id, username in [(owner_id, owner_name), (member_id, member_name)]:
        db.execute(
            insert(users).values(
                id=user_id,
                username=username,
                email=f"{username}@t.invalid",
                password_hash="x",
                bio=username,
                created_at=now,
                updated_at=now,
            )
        )

    channel_id = uuid4()
    channel_slug = f"cvt-ch-{str(channel_id)[:8]}"
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name=channel_slug,
            description="conversion test",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    project_id = uuid4()
    project_slug = f"cvt-proj-{str(project_id)[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Convertible Project",
            description="seed",
            author_id=owner_id,
            project_mode=mode,
            project_subtype=subtype,
            current_phase_id="phase-5",
            stage_label="Activity",
            location_label="online",
            is_platform_tagged=False,
            is_closed=False,
            signal_count=0,
            vote_count=0,
            comment_count=0,
            member_count=2,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(project_tags).values(
            project_id=project_id,
            tag_kind="channel",
            channel_id=channel_id,
            community_id=None,
        )
    )
    for user_id in [owner_id, member_id]:
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
    db.close()
    return {
        "owner_id": owner_id,
        "member_id": member_id,
        "owner_token": create_access_token(str(owner_id)),
        "member_token": create_access_token(str(member_id)),
        "project_slug": project_slug,
        "project_id": project_id,
        "channel_id": channel_id,
    }


def test_close_sets_is_closed_and_drops_from_feed():
    seeded = _seed_project()
    with TestClient(app) as client:
        propose = client.post(
            f"/projects/{seeded['project_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "target_phase_id": "phase-7",
                "reason": "Done.",
                "close_outcome": "close",
            },
        )
        assert propose.status_code == 200, propose.text
        request_id = propose.json()["request"]["id"]

        executed = False
        last_vote = None
        for token in (seeded["owner_token"], seeded["member_token"]):
            last_vote = client.post(
                f"/projects/{seeded['project_slug']}/phase-requests/{request_id}/vote",
                headers=_auth_header(token),
                json={"vote": "yes"},
            )
            if last_vote.status_code == 409 and executed:
                break
            assert last_vote.status_code == 200, last_vote.text
            executed = bool(last_vote.json().get("executed"))
            if executed:
                break

        assert last_vote is not None
        assert executed is True
        assert last_vote.json()["current_phase_id"] == "phase-7"

        detail = client.get(f"/projects/{seeded['project_slug']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["lifecycle"]["currentPhaseId"] == "phase-7"

        db = SessionLocal()
        row = (
            db.execute(select(projects).where(projects.c.id == seeded["project_id"]))
            .mappings()
            .one()
        )
        assert row["is_closed"] is True
        assert row["close_outcome"] == "close"
        db.close()

        feed = client.get("/feeds/public?filter=projects&sort=recent")
        assert feed.status_code == 200, feed.text
        slugs = [item["slug"] for item in feed.json()["items"] if item["entity_type"] == "project"]
        assert seeded["project_slug"] not in slugs


def test_conversion_creates_successor_lineage_and_inherited_history():
    seeded = _seed_project()
    with TestClient(app) as client:
        propose = client.post(
            f"/projects/{seeded['project_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "target_phase_id": "phase-7",
                "reason": "Become a service.",
                "close_outcome": "convert",
                "conversion_target_mode": "collective-service",
                "conversion_target_subtype": "standard",
                "conversion_successor_title": "Converted Service",
                "conversion_successor_description": "Ongoing collective service",
            },
        )
        assert propose.status_code == 200, propose.text
        request_id = propose.json()["request"]["id"]

        executed = False
        last_vote = None
        for token in (seeded["owner_token"], seeded["member_token"]):
            last_vote = client.post(
                f"/projects/{seeded['project_slug']}/phase-requests/{request_id}/vote",
                headers=_auth_header(token),
                json={"vote": "yes"},
            )
            if last_vote.status_code == 409 and executed:
                break
            assert last_vote.status_code == 200, last_vote.text
            executed = bool(last_vote.json().get("executed"))
            if executed:
                break

        assert last_vote is not None
        assert executed is True

        db = SessionLocal()
        pred = (
            db.execute(select(projects).where(projects.c.id == seeded["project_id"]))
            .mappings()
            .one()
        )
        assert pred["is_closed"] is True
        assert pred["close_outcome"] == "convert"

        conversion = (
            db.execute(
                select(project_conversions).where(
                    project_conversions.c.predecessor_project_id == seeded["project_id"]
                )
            )
            .mappings()
            .one()
        )
        successor = (
            db.execute(select(projects).where(projects.c.id == conversion["successor_project_id"]))
            .mappings()
            .one()
        )
        assert successor["title"] == "Converted Service"
        assert successor["project_mode"] == "collective-service"
        assert successor["current_phase_id"] == "phase-1"
        assert successor["is_closed"] is False

        tags = (
            db.execute(select(project_tags).where(project_tags.c.project_id == successor["id"]))
            .mappings()
            .all()
        )
        assert any(tag["channel_id"] == seeded["channel_id"] for tag in tags)

        members = (
            db.execute(
                select(project_memberships).where(
                    project_memberships.c.project_id == successor["id"]
                )
            )
            .mappings()
            .all()
        )
        assert {m["user_id"] for m in members} == {seeded["owner_id"], seeded["member_id"]}

        links = (
            db.execute(
                select(project_links).where(
                    (
                        (project_links.c.source_project_id == seeded["project_id"])
                        & (project_links.c.target_project_id == successor["id"])
                    )
                    | (
                        (project_links.c.source_project_id == successor["id"])
                        & (project_links.c.target_project_id == seeded["project_id"])
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(links) == 2
        assert {link["link_kind"] for link in links} == {"conversion"}
        labels = {link["relationship_label"] for link in links}
        assert "Converted to" in labels
        assert "Converted from" in labels

        inherited = (
            db.execute(
                select(project_inherited_decisions).where(
                    project_inherited_decisions.c.successor_project_id == successor["id"]
                )
            )
            .mappings()
            .all()
        )
        assert inherited
        assert all(item["predecessor_slug"] == seeded["project_slug"] for item in inherited)
        db.close()

        pred_links = client.get(f"/projects/{seeded['project_slug']}/links")
        assert pred_links.status_code == 200, pred_links.text
        lineage = pred_links.json()["linksFrame"]["conversionLineage"]
        assert lineage is not None
        assert lineage["successor"]["href"] == f"/projects/{successor['slug']}"

        succ_history = client.get(f"/projects/{successor['slug']}/history")
        assert succ_history.status_code == 200, succ_history.text
        history = succ_history.json()["history"]
        inherited_entries = [item for item in history if item.get("isInherited")]
        assert inherited_entries
        assert inherited_entries[0]["originPredecessorSlug"] == seeded["project_slug"]


def test_duplicate_conversion_is_rejected():
    seeded = _seed_project()
    with TestClient(app) as client:
        propose = client.post(
            f"/projects/{seeded['project_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "target_phase_id": "phase-7",
                "reason": "Convert once",
                "close_outcome": "convert",
                "conversion_target_mode": "collective-service",
                "conversion_target_subtype": "standard",
            },
        )
        assert propose.status_code == 200, propose.text
        request_id = propose.json()["request"]["id"]
        executed = False
        for token in (seeded["owner_token"], seeded["member_token"]):
            vote = client.post(
                f"/projects/{seeded['project_slug']}/phase-requests/{request_id}/vote",
                headers=_auth_header(token),
                json={"vote": "yes"},
            )
            if vote.status_code == 409 and executed:
                break
            assert vote.status_code == 200, vote.text
            executed = bool(vote.json().get("executed"))
            if executed:
                break
        assert executed is True

        # Closed projects cannot accept another convert request.
        again = client.post(
            f"/projects/{seeded['project_slug']}/phase-requests",
            headers=_auth_header(seeded["owner_token"]),
            json={
                "target_phase_id": "phase-7",
                "reason": "Convert twice",
                "close_outcome": "convert",
                "conversion_target_mode": "collective-service",
                "conversion_target_subtype": "standard",
            },
        )
        assert again.status_code == 409, again.text
