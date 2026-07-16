"""Activity history rating, completion, and staffing gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.main import app
from app.models import (
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_memberships,
    events,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_memberships,
    project_service_requests,
    projects,
    users,
)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user(db: Session, now: datetime) -> tuple[UUID, str]:
    user_id = uuid4()
    username = f"hist-{user_id.hex[:8]}"
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


def _seed_project(db: Session, now: datetime, owner_id: UUID) -> tuple[UUID, str]:
    project_id = uuid4()
    slug = f"hist-proj-{project_id.hex[:8]}"
    db.execute(
        insert(projects).values(
            id=project_id,
            slug=slug,
            title="History test project",
            description="seed",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="standard",
            current_phase_id="phase-5",
            stage_label="activity",
            location_label="online",
            is_platform_tagged=False,
            is_closed=False,
            signal_count=0,
            vote_count=0,
            comment_count=0,
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=owner_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    return project_id, slug


def _seed_event(db: Session, now: datetime, owner_id: UUID) -> tuple[UUID, str]:
    event_id = uuid4()
    slug = f"hist-evt-{event_id.hex[:8]}"
    db.execute(
        insert(events).values(
            id=event_id,
            slug=slug,
            title="History test event",
            description="seed",
            created_by=owner_id,
            is_private=False,
            current_phase_id="activity",
            time_label="Past",
            location_label="Workshop",
            scheduled_at=now,
            vote_count=0,
            comment_count=0,
            going_count=0,
            member_count=1,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )
    db.execute(
        insert(event_memberships).values(
            event_id=event_id,
            user_id=owner_id,
            role="member",
            joined_at=now,
        )
    )
    return event_id, slug


def _create_ended_project_activity(
    db: Session,
    *,
    project_id: UUID,
    author_id: UUID,
    now: datetime,
    committed_user_ids: list[UUID],
    required_count: int = 1,
) -> UUID:
    activity_id = uuid4()
    role_id = uuid4()
    db.execute(
        insert(project_activities).values(
            id=activity_id,
            project_id=project_id,
            linked_plan_id=None,
            linked_plan_phase_id=None,
            linked_request_id=None,
            title="Ended activity",
            author_id=author_id,
            scheduled_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            is_online=False,
            location_label="Room A",
            note="history test",
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(project_activity_roles).values(
            id=role_id,
            activity_id=activity_id,
            label="Helper",
            required_count=required_count,
            maximum_count=None,
            created_at=now,
        )
    )
    for user_id in committed_user_ids:
        db.execute(
            insert(project_activity_assignments).values(
                role_id=role_id,
                user_id=user_id,
                created_at=now,
            )
        )
    return activity_id


def _link_service_request(
    db: Session,
    *,
    project_id: UUID,
    requester_id: UUID,
    activity_id: UUID,
    now: datetime,
) -> UUID:
    request_id = uuid4()
    db.execute(
        insert(project_service_requests).values(
            id=request_id,
            project_id=project_id,
            requester_id=requester_id,
            title="Service request",
            body="Please help",
            status="accepted",
            scheduled_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            linked_activity_id=activity_id,
            created_at=now,
            updated_at=now,
        )
    )
    return request_id


def _create_ended_event_activity(
    db: Session,
    *,
    event_id: UUID,
    author_id: UUID,
    now: datetime,
    committed_user_ids: list[UUID],
    required_count: int = 1,
) -> UUID:
    activity_id = uuid4()
    role_id = uuid4()
    db.execute(
        insert(event_activities).values(
            id=activity_id,
            event_id=event_id,
            linked_plan_id=None,
            linked_plan_phase_id=None,
            title="Ended event activity",
            author_id=author_id,
            scheduled_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            is_online=False,
            location_label="Room B",
            note="history test",
            created_at=now,
        )
    )
    db.execute(
        insert(event_activity_roles).values(
            id=role_id,
            activity_id=activity_id,
            label="Helper",
            required_count=required_count,
            maximum_count=None,
            created_at=now,
        )
    )
    for user_id in committed_user_ids:
        db.execute(
            insert(event_activity_assignments).values(
                role_id=role_id,
                user_id=user_id,
                created_at=now,
            )
        )
    return activity_id


def _project_history_item(slug: str, token: str, activity_id: UUID) -> dict[str, object]:
    with TestClient(app) as client:
        response = client.get(f"/projects/{slug}", headers=_auth_header(token))
    assert response.status_code == 200
    history = response.json()["lifecycle"]["phaseFive"]["history"]
    item = next(entry for entry in history if entry["id"] == str(activity_id))
    return item


def _event_history_item(slug: str, token: str, activity_id: UUID) -> dict[str, object]:
    with TestClient(app) as client:
        response = client.get(f"/events/{slug}", headers=_auth_header(token))
    assert response.status_code == 200
    history = response.json()["lifecycle"]["activity"]["history"]
    item = next(entry for entry in history if entry["id"] == str(activity_id))
    return item


def test_assigned_user_can_rate_and_complete_staffed_activity() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    item = _project_history_item(slug, token, activity_id)
    assert item["viewerCanRate"] is True
    participant_completion = item["participantCompletion"]
    assert participant_completion["viewerCanSet"] is True
    assert participant_completion.get("systemAutoUncompleted") is not True

    with TestClient(app) as client:
        completion_response = client.post(
            f"/projects/{slug}/service-history/{activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )
    assert completion_response.status_code == 200


def test_zero_committed_activity_is_auto_uncompleted() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    item = _project_history_item(slug, token, activity_id)
    participant_completion = item["participantCompletion"]
    assert participant_completion["systemAutoUncompleted"] is True
    assert participant_completion["viewerCanSet"] is False
    assert participant_completion["statusLabel"] == "Marked uncompleted — no participants signed up"

    db = SessionLocal()
    row = db.execute(
        select(project_activities.c.participant_auto_uncompleted_at).where(
            project_activities.c.id == activity_id
        )
    ).first()
    assert row is not None
    assert row[0] is not None
    db.close()


def test_under_minimum_staffing_allows_completion() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=2,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    item = _project_history_item(slug, token, activity_id)
    participant_completion = item["participantCompletion"]
    assert participant_completion.get("systemAutoUncompleted") is not True
    assert participant_completion["viewerCanSet"] is True

    with TestClient(app) as client:
        response = client.post(
            f"/projects/{slug}/service-history/{activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )
    assert response.status_code == 200


def test_completion_api_rejects_zero_committed() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{slug}/service-history/{activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )
    assert response.status_code == 403


def test_event_detail_loads_with_ended_history() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    event_id, slug = _seed_event(db, now, owner_id)
    _create_ended_event_activity(
        db,
        event_id=event_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        response = client.get(f"/events/{slug}", headers=_auth_header(token))
    assert response.status_code == 200
    history = response.json()["lifecycle"]["activity"]["history"]
    assert len(history) == 1


def test_event_history_applies_staffing_rules() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    event_id, slug = _seed_event(db, now, owner_id)
    staffed_activity_id = _create_ended_event_activity(
        db,
        event_id=event_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    unstaffed_activity_id = _create_ended_event_activity(
        db,
        event_id=event_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    staffed_item = _event_history_item(slug, token, staffed_activity_id)
    unstaffed_item = _event_history_item(slug, token, unstaffed_activity_id)

    assert staffed_item["viewerCanRate"] is True
    assert staffed_item["participantCompletion"]["viewerCanSet"] is True
    assert unstaffed_item["participantCompletion"]["systemAutoUncompleted"] is True
    assert unstaffed_item["participantCompletion"]["viewerCanSet"] is False

    with TestClient(app) as client:
        staffed_completion = client.post(
            f"/events/{slug}/activity-history/{staffed_activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )
        blocked_completion = client.post(
            f"/events/{slug}/activity-history/{unstaffed_activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )

    assert staffed_completion.status_code == 200
    assert blocked_completion.status_code == 403

    reloaded = _event_history_item(slug, token, staffed_activity_id)
    assert reloaded["participantCompletion"]["viewerSelection"] == "completed"


def test_project_activity_rating_persists_on_detail() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        rating_response = client.put(
            f"/projects/{slug}/activities/{activity_id}/rating",
            headers=_auth_header(token),
            json={"rating": 4, "comment": "Solid session"},
        )
    assert rating_response.status_code == 200

    item = _project_history_item(slug, token, activity_id)
    assert item["viewerCanRate"] is True
    assert item["viewerRating"] == {"rating": 4, "comment": "Solid session"}
    assert item["aggregateRating"]["count"] == 1
    assert item["aggregateRating"]["average"] == 4.0
    assert len(item["ratings"]) == 1
    assert item["ratings"][0]["rating"] == 4
    assert item["ratings"][0]["comment"] == "Solid session"
    assert item["ratings"][0]["roleLabel"] == "Participant"


def test_requester_can_rate_service_activity_with_role_label() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    provider_id, _ = _seed_user(db, now)
    requester_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, provider_id)
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=requester_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=provider_id,
        now=now,
        committed_user_ids=[provider_id],
        required_count=1,
    )
    _link_service_request(
        db,
        project_id=project_id,
        requester_id=requester_id,
        activity_id=activity_id,
        now=now,
    )
    db.commit()
    db.close()

    requester_token = create_access_token(str(requester_id))
    provider_token = create_access_token(str(provider_id))

    with TestClient(app) as client:
        requester_rating = client.put(
            f"/projects/{slug}/activities/{activity_id}/rating",
            headers=_auth_header(requester_token),
            json={"rating": 5, "comment": "Great service"},
        )
        provider_rating = client.put(
            f"/projects/{slug}/activities/{activity_id}/rating",
            headers=_auth_header(provider_token),
            json={"rating": 4, "comment": "Went well"},
        )

    assert requester_rating.status_code == 200
    assert provider_rating.status_code == 200

    item = _project_history_item(slug, requester_token, activity_id)
    assert item["viewerCanRate"] is True
    assert item["requesterCompletion"] is not None
    ratings_by_user = {entry["userId"]: entry for entry in item["ratings"]}
    assert ratings_by_user[str(requester_id)]["roleLabel"] == "Requester"
    assert ratings_by_user[str(provider_id)]["roleLabel"] == "Participant"


def test_non_participant_non_requester_cannot_rate() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    provider_id, _ = _seed_user(db, now)
    requester_id, _ = _seed_user(db, now)
    outsider_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, provider_id)
    for member_id in (requester_id, outsider_id):
        db.execute(
            insert(project_memberships).values(
                project_id=project_id,
                user_id=member_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
            )
        )
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=provider_id,
        now=now,
        committed_user_ids=[provider_id],
        required_count=1,
    )
    _link_service_request(
        db,
        project_id=project_id,
        requester_id=requester_id,
        activity_id=activity_id,
        now=now,
    )
    db.commit()
    db.close()

    outsider_token = create_access_token(str(outsider_id))
    with TestClient(app) as client:
        response = client.put(
            f"/projects/{slug}/activities/{activity_id}/rating",
            headers=_auth_header(outsider_token),
            json={"rating": 2, "comment": "Should fail"},
        )

    assert response.status_code == 403


def test_completion_same_selection_is_idempotent() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    project_id, slug = _seed_project(db, now, owner_id)
    activity_id = _create_ended_project_activity(
        db,
        project_id=project_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        first = client.post(
            f"/projects/{slug}/service-history/{activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )
        second = client.post(
            f"/projects/{slug}/service-history/{activity_id}/completion",
            headers=_auth_header(token),
            json={"role": "participants", "selection": "completed"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["selection"] == "completed"

    item = _project_history_item(slug, token, activity_id)
    assert item["participantCompletion"]["viewerSelection"] == "completed"


def test_event_activity_rating_persists_on_detail() -> None:
    db = SessionLocal()
    now = datetime.now(UTC)
    owner_id, _ = _seed_user(db, now)
    event_id, slug = _seed_event(db, now, owner_id)
    activity_id = _create_ended_event_activity(
        db,
        event_id=event_id,
        author_id=owner_id,
        now=now,
        committed_user_ids=[owner_id],
        required_count=1,
    )
    db.commit()
    db.close()

    token = create_access_token(str(owner_id))
    with TestClient(app) as client:
        rating_response = client.put(
            f"/events/{slug}/activities/{activity_id}/rating",
            headers=_auth_header(token),
            json={"rating": 5, "comment": "Great event activity"},
        )
    assert rating_response.status_code == 200

    item = _event_history_item(slug, token, activity_id)
    assert item["viewerCanRate"] is True
    assert item["viewerRating"] == {"rating": 5, "comment": "Great event activity"}
    assert item["aggregateRating"]["count"] == 1
    assert item["aggregateRating"]["average"] == 5.0
    assert len(item["ratings"]) == 1
    assert item["ratings"][0]["rating"] == 5
    assert item["ratings"][0]["comment"] == "Great event activity"
    assert item["ratings"][0]["roleLabel"] == "Participant"
