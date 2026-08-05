from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import project_signals, projects, users
from app.services.signal_gates import (
    build_proposal_signal_summary,
    proposal_advancement_unlocked,
)


def test_non_platform_requires_ratio_above_66_percent() -> None:
    assert (
        proposal_advancement_unlocked(
            {"demand": 1, "opposition": 1, "total": 2},
            required_demand=1,
            uses_platform_vote_context=False,
        )
        is False
    )

    assert (
        proposal_advancement_unlocked(
            {"demand": 2, "opposition": 1, "total": 3},
            required_demand=1,
            uses_platform_vote_context=False,
        )
        is True
    )


def test_platform_requires_ratio_and_demand_count() -> None:
    assert (
        proposal_advancement_unlocked(
            {"demand": 2, "opposition": 1, "total": 3},
            required_demand=3,
            uses_platform_vote_context=True,
        )
        is False
    )

    assert (
        proposal_advancement_unlocked(
            {"demand": 3, "opposition": 0, "total": 3},
            required_demand=3,
            uses_platform_vote_context=True,
        )
        is True
    )


def test_build_proposal_signal_summary_marks_ratio_gate() -> None:
    summary = build_proposal_signal_summary(
        {"demand": 1, "opposition": 1, "total": 2},
        required_demand=1,
        uses_platform_vote_context=False,
        viewer_signal=None,
        vote_context_label="project members",
        vote_context_population=4,
    )

    assert summary["signalRatioPercent"] == 50.0
    assert summary["ratioRequirementMet"] is False
    assert summary["advancementUnlocked"] is False


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_project_with_signals(
    db,
    *,
    owner_id,
    supporter_id,
    now: datetime,
) -> tuple[str, str]:
    project_id = uuid4()
    slug = f"gate-{project_id.hex[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="Signal gate project",
            description="test",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-1",
            stage_label="Proposal",
            location_label="online",
            is_platform_tagged=False,
            is_closed=False,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(project_signals).values(
            project_id=project_id,
            user_id=supporter_id,
            signal_type="demand",
        )
    )
    db.execute(
        insert(project_signals).values(
            project_id=project_id,
            user_id=owner_id,
            signal_type="opposition",
        )
    )
    return slug, project_id


def test_project_phase_advance_request_rejects_fifty_fifty_signals() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id = uuid4()
    supporter_id = uuid4()
    db.execute(
        insert(users).values(
            id=owner_id,
            username=f"owner-{owner_id.hex[:8]}",
            email=f"{owner_id.hex[:8]}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=supporter_id,
            username=f"supporter-{supporter_id.hex[:8]}",
            email=f"{supporter_id.hex[:8]}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    slug, project_id = _seed_project_with_signals(
        db, owner_id=owner_id, supporter_id=supporter_id, now=now
    )
    from app.models import project_memberships

    for user_id in (owner_id, supporter_id):
        db.execute(
            insert(project_memberships).values(
                project_id=project_id,
                user_id=user_id,
                is_manager=user_id == owner_id,
                is_manager_candidate=False,
                joined_at=now,
            )
        )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{slug}/phase-requests",
            headers=_auth_header(token),
            json={"target_phase_id": "phase-2", "reason": "Ready to plan"},
        )

    assert response.status_code == 409
    assert "66%" in response.json()["detail"]


@pytest.mark.parametrize(
    ("signals", "required_demand", "uses_platform", "expected"),
    [
        ({"demand": 0, "opposition": 0, "total": 0}, 1, False, False),
        ({"demand": 1, "opposition": 0, "total": 1}, 1, False, True),
        ({"demand": 1, "opposition": 1, "total": 2}, 1, False, False),
    ],
)
def test_proposal_advancement_unlocked_matrix(
    signals: dict[str, int],
    required_demand: int,
    uses_platform: bool,
    expected: bool,
) -> None:
    assert (
        proposal_advancement_unlocked(
            signals,
            required_demand=required_demand,
            uses_platform_vote_context=uses_platform,
        )
        is expected
    )
