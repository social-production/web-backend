from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import channels, projects, scope_memberships, users
from app.services.projects_phases import display_stage_label, visible_phase_ids_for_project


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed() -> dict[str, object]:
    db = SessionLocal()
    now = datetime.now(UTC)

    creator_id = uuid4()
    channel_id = uuid4()
    creator_name = f"phase-creator-{str(creator_id)[:8]}"
    channel_slug = f"phase-ch-{str(channel_id)[:8]}"

    db.execute(
        insert(users).values(
            id=creator_id,
            username=creator_name,
            email=f"{creator_name}@t.invalid",
            password_hash="x",
            bio=creator_name,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Phase Channel",
            description="seed",
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="channel",
            scope_id=channel_id,
            user_id=creator_id,
            role="member",
            created_at=now,
        )
    )
    db.commit()
    db.close()

    return {
        "creator_token": create_access_token(str(creator_id)),
        "channel_slug": channel_slug,
    }


def run() -> None:
    seeded = _seed()
    personal_slug = f"phase-ps-{str(uuid4())[:8]}"
    software_slug = f"phase-sw-{str(uuid4())[:8]}"

    assert display_stage_label("personal-service", None, "phase-1") == "Activity"
    assert display_stage_label("productive", None, "phase-1") == "Proposal"
    assert visible_phase_ids_for_project("personal-service", None, "phase-1") == [
        "phase-1",
        "phase-2",
    ]
    assert visible_phase_ids_for_project("collective-service", None, "phase-3") == [
        "phase-1",
        "phase-2",
        "phase-5",
        "phase-7",
    ]
    assert visible_phase_ids_for_project("productive", "software", "phase-3") == [
        "phase-1",
        "phase-2",
        "phase-5",
        "phase-7",
    ]
    assert "phase-6" not in visible_phase_ids_for_project("productive", "software", "phase-3")

    with TestClient(app) as client:
        personal = client.post(
            "/projects",
            headers=_auth_header(seeded["creator_token"]),
            json={
                "slug": personal_slug,
                "title": "Personal Phase Test",
                "description": "Personal service phase naming.",
                "project_mode": "personal-service",
                "location_label": "Online",
                "channel_slugs": [seeded["channel_slug"]],
                "request_mode": "direct",
            },
        )
        assert personal.status_code == 200, personal.text

        personal_detail = client.get(
            f"/projects/{personal_slug}", headers=_auth_header(seeded["creator_token"])
        )
        assert personal_detail.status_code == 200, personal_detail.text
        personal_payload = personal_detail.json()
        assert len(personal_payload["lifecycle"]["phases"]) == 2
        assert [phase["title"] for phase in personal_payload["lifecycle"]["phases"]] == [
            "Activity",
            "Closed",
        ]
        assert personal_payload["stage"] == "Activity"

        software = client.post(
            "/projects",
            headers=_auth_header(seeded["creator_token"]),
            json={
                "slug": software_slug,
                "title": "Software Phase Test",
                "description": "Productive software phase naming.",
                "project_mode": "productive",
                "location_label": "Online",
                "channel_slugs": [seeded["channel_slug"]],
            },
        )
        assert software.status_code == 200, software.text

        db = SessionLocal()
        db.execute(
            update(projects)
            .where(projects.c.slug == software_slug)
            .values(
                project_subtype="software",
                current_phase_id="phase-3",
                stage_label="Distribution Plan",
            )
        )
        db.commit()
        db.close()

        software_detail = client.get(
            f"/projects/{software_slug}", headers=_auth_header(seeded["creator_token"])
        )
        assert software_detail.status_code == 200, software_detail.text
        software_payload = software_detail.json()
        software_phase_ids = [phase["id"] for phase in software_payload["lifecycle"]["phases"]]
        assert software_phase_ids == ["phase-1", "phase-2", "phase-5", "phase-7"]
        assert software_payload["lifecycle"]["phases"][0]["title"] == "Proposal"

        home_feed = client.get("/feeds/home", headers=_auth_header(seeded["creator_token"]))
        assert home_feed.status_code == 200, home_feed.text
        personal_feed_item = next(
            item for item in home_feed.json()["items"] if item.get("slug") == personal_slug
        )
        assert personal_feed_item["stage_label"] == "Activity"

        print(
            json.dumps(
                {
                    "personal_phases": [
                        phase["title"] for phase in personal_payload["lifecycle"]["phases"]
                    ],
                    "software_phase_ids": software_phase_ids,
                    "personal_feed_stage": personal_feed_item["stage_label"],
                }
            )
        )


if __name__ == "__main__":
    run()
