from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

# Ensure "app" imports work when running: python scripts/seed.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.auth.passwords import hash_password
from app.config import get_settings
from app.models import (
    channels,
    detail_link_request_votes,
    detail_link_requests,
    detail_links,
    events,
    help_requests,
    locations,
    project_activities,
    project_service_request_settings,
    projects,
    scope_memberships,
    user_settings,
    users,
)
from app.services.content.help_requests import create_help_request
from app.services.events.helpers import create_event
from app.services.locations.model import create_location
from app.services.projects.actions import create_project_activity
from app.services.projects.helpers import create_project
from app.services.projects.phases.constants import STAGE_LABEL_BY_PHASE_ID


PLATFORM_SLUG = "platform"
VICTORIA_DEMO_USERNAME = "victoria-demo"
VICTORIA_DEMO_PASSWORD = "victoria-demo-dev"
VICTORIA_CHANNEL_SLUG = "victoria"


VICTORIA_PLACES: dict[str, dict[str, object]] = {
    "federation-square": {
        "display_label": "Federation Square, Melbourne VIC",
        "latitude": -37.8183,
        "longitude": 144.9671,
    },
    "brunswick": {
        "display_label": "Brunswick Town Hall, Brunswick VIC",
        "latitude": -37.7676,
        "longitude": 144.9607,
    },
    "edinburgh-gardens": {
        "display_label": "Edinburgh Gardens, Fitzroy VIC",
        "latitude": -37.7910,
        "longitude": 144.9795,
    },
    "carlton-gardens": {
        "display_label": "Carlton Gardens, Carlton VIC",
        "latitude": -37.8055,
        "longitude": 144.9710,
    },
    "collingwood": {
        "display_label": "Collingwood Town Hall, Collingwood VIC",
        "latitude": -37.8040,
        "longitude": 144.9870,
    },
    "botanic-gardens": {
        "display_label": "Royal Botanic Gardens Victoria, Melbourne VIC",
        "latitude": -37.8304,
        "longitude": 144.9800,
    },
}


def seed_platform_channel() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)

    with engine.begin() as conn:
        existing_id = conn.execute(
            sa.select(channels.c.id).where(channels.c.slug == PLATFORM_SLUG).limit(1)
        ).scalar_one_or_none()

        if existing_id is not None:
            print(f"SKIPPED channels slug={PLATFORM_SLUG} (already exists)")
            return

        conn.execute(
            sa.insert(channels).values(
                slug=PLATFORM_SLUG,
                name="Platform",
                description="The platform governance channel",
                created_by=None,
            )
        )
        print(f"CREATED channels slug={PLATFORM_SLUG}")


def _ensure_demo_user(db: Session) -> UUID:
    existing = (
        db.execute(select(users.c.id).where(users.c.username == VICTORIA_DEMO_USERNAME))
        .scalars()
        .first()
    )
    if existing is not None:
        settings_row = db.execute(
            select(user_settings.c.user_id).where(user_settings.c.user_id == existing)
        ).first()
        if settings_row is None:
            db.execute(sa.insert(user_settings).values(user_id=existing))
            db.commit()
            print(f"CREATED user_settings username={VICTORIA_DEMO_USERNAME}")
        print(f"SKIPPED users username={VICTORIA_DEMO_USERNAME}")
        return existing

    now = datetime.now(UTC)
    user_id = db.execute(
        sa.insert(users)
        .values(
            username=VICTORIA_DEMO_USERNAME,
            email=f"{VICTORIA_DEMO_USERNAME}@example.invalid",
            password_hash=hash_password(VICTORIA_DEMO_PASSWORD),
            bio="Victoria map sample account for local development.",
            created_at=now,
            updated_at=now,
        )
        .returning(users.c.id)
    ).scalar_one()
    db.execute(sa.insert(user_settings).values(user_id=user_id))
    db.commit()
    print(f"CREATED users username={VICTORIA_DEMO_USERNAME}")
    return user_id


def _ensure_victoria_channel(db: Session, creator_id: UUID) -> str:
    existing = (
        db.execute(
            select(channels.c.id, channels.c.slug).where(
                channels.c.slug == VICTORIA_CHANNEL_SLUG
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        membership = db.execute(
            select(scope_memberships.c.id).where(
                scope_memberships.c.scope_kind == "channel",
                scope_memberships.c.scope_id == existing["id"],
                scope_memberships.c.user_id == creator_id,
            )
        ).first()
        if membership is None:
            db.execute(
                sa.insert(scope_memberships).values(
                    scope_kind="channel",
                    scope_id=existing["id"],
                    user_id=creator_id,
                    role="member",
                )
            )
            db.commit()
        print(f"SKIPPED channels slug={VICTORIA_CHANNEL_SLUG}")
        return existing["slug"]

    now = datetime.now(UTC)
    channel_id = db.execute(
        sa.insert(channels)
        .values(
            slug=VICTORIA_CHANNEL_SLUG,
            name="Victoria",
            description="Local Victoria discovery channel for map sample data.",
            created_by=creator_id,
            created_at=now,
            updated_at=now,
        )
        .returning(channels.c.id)
    ).scalar_one()
    db.execute(
        sa.insert(scope_memberships).values(
            scope_kind="channel",
            scope_id=channel_id,
            user_id=creator_id,
            role="member",
        )
    )
    db.commit()
    print(f"CREATED channels slug={VICTORIA_CHANNEL_SLUG}")
    return VICTORIA_CHANNEL_SLUG


def _ensure_location(db: Session, place_key: str) -> tuple[UUID, str]:
    place = VICTORIA_PLACES[place_key]
    provider_place_id = f"seed:victoria:{place_key}"
    existing = (
        db.execute(
            select(locations.c.id, locations.c.display_label).where(
                locations.c.provider_place_id == provider_place_id
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        print(f"SKIPPED locations key={place_key}")
        return existing["id"], existing["display_label"]

    created = create_location(
        db,
        {
            "display_label": place["display_label"],
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "region": "Victoria",
            "country": "Australia",
            "precision": "approximate",
            "is_online": False,
            "provider_place_id": provider_place_id,
        },
    )
    db.commit()
    print(f"CREATED locations key={place_key}")
    return created["id"], created["display_label"]


def _find_project_by_title(db: Session, title: str) -> dict[str, object] | None:
    return (
        db.execute(
            select(
                projects.c.id,
                projects.c.slug,
                projects.c.title,
                projects.c.project_mode,
                projects.c.current_phase_id,
            ).where(projects.c.title == title)
        )
        .mappings()
        .first()
    )


def _find_event_by_title(db: Session, title: str) -> dict[str, object] | None:
    return (
        db.execute(select(events.c.id, events.c.slug, events.c.title).where(events.c.title == title))
        .mappings()
        .first()
    )


def _find_help_by_title(db: Session, title: str) -> dict[str, object] | None:
    return (
        db.execute(select(help_requests.c.id, help_requests.c.title).where(help_requests.c.title == title))
        .mappings()
        .first()
    )


def _find_activity_by_title(db: Session, project_id: UUID, title: str) -> dict[str, object] | None:
    return (
        db.execute(
            select(project_activities.c.id, project_activities.c.title).where(
                project_activities.c.project_id == project_id,
                project_activities.c.title == title,
            )
        )
        .mappings()
        .first()
    )


def _advance_project_phase(db: Session, project_id: UUID, phase_id: str) -> None:
    stage_label = STAGE_LABEL_BY_PHASE_ID.get(phase_id, phase_id)
    db.execute(
        update(projects)
        .where(projects.c.id == project_id)
        .values(current_phase_id=phase_id, stage_label=stage_label)
    )
    db.commit()


def _ensure_collective_requests_enabled(db: Session, project_id: UUID) -> None:
    existing = db.execute(
        select(project_service_request_settings.c.project_id).where(
            project_service_request_settings.c.project_id == project_id
        )
    ).first()
    if existing is None:
        db.execute(
            sa.insert(project_service_request_settings).values(
                project_id=project_id,
                enabled=True,
                request_mode="both",
                allow_off_schedule_requests=True,
                summary="Accepting service requests for map testing.",
            )
        )
    else:
        db.execute(
            update(project_service_request_settings)
            .where(project_service_request_settings.c.project_id == project_id)
            .values(
                enabled=True,
                request_mode="both",
                allow_off_schedule_requests=True,
            )
        )
    db.commit()


def seed_victoria_map_samples() -> None:
    """Idempotent Victoria sample set for local map testing."""
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    now = datetime.now(UTC)

    with Session(engine) as db:
        user_id = _ensure_demo_user(db)
        channel_slug = _ensure_victoria_channel(db, user_id)

        location_ids: dict[str, UUID] = {}
        location_labels: dict[str, str] = {}
        for key in VICTORIA_PLACES:
            location_id, label = _ensure_location(db, key)
            location_ids[key] = location_id
            location_labels[key] = label

        event_title = "Federation Square Community Picnic"
        if _find_event_by_title(db, event_title) is None:
            create_event(
                db,
                current_user_id=user_id,
                title=event_title,
                description="Open picnic and meetup near the CBD for local organisers.",
                is_private=False,
                time_label="This afternoon",
                location_label=location_labels["federation-square"],
                channel_slugs=[channel_slug],
                community_slugs=[],
                scheduled_at=now + timedelta(hours=5),
                audience="public",
                governance="collaborative",
                location_id=location_ids["federation-square"],
            )
            print(f"CREATED events title={event_title}")
        else:
            print(f"SKIPPED events title={event_title}")

        help_title = "Garden bed reset at Royal Botanic Gardens"
        if _find_help_by_title(db, help_title) is None:
            create_help_request(
                db,
                current_user_id=user_id,
                title=help_title,
                body="Looking for a few people to help reset a community garden bed later today.",
                location_label=location_labels["botanic-gardens"],
                needed_at=now + timedelta(hours=7),
                roles=[{"title": "Helper", "slots": 3, "description": "Bring gloves if you have them"}],
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_ids["botanic-gardens"],
            )
            print(f"CREATED help_requests title={help_title}")
        else:
            print(f"SKIPPED help_requests title={help_title}")

        productive_title = "Brunswick Tool Library Build"
        productive = _find_project_by_title(db, productive_title)
        if productive is None:
            created = create_project(
                db,
                current_user_id=user_id,
                title=productive_title,
                description="Shared tool library workshop past the proposal stage, based in Brunswick.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_labels["brunswick"],
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_ids["brunswick"],
            )["project"]
            productive_id = created["id"]
            productive_slug = str(created["slug"])
            _advance_project_phase(db, productive_id, "phase-5")
            print(f"CREATED projects title={productive_title} phase=phase-5")
        else:
            productive_id = productive["id"]
            productive_slug = str(productive["slug"])
            if productive["current_phase_id"] == "phase-1":
                _advance_project_phase(db, productive_id, "phase-5")
            print(f"SKIPPED projects title={productive_title}")

        productive_activity_title = "Brunswick Saturday Build Session"
        if _find_activity_by_title(db, productive_id, productive_activity_title) is None:
            create_project_activity(
                db,
                current_user_id=user_id,
                slug=productive_slug,
                title=productive_activity_title,
                scheduled_at=now + timedelta(hours=8),
                ends_at=now + timedelta(hours=11),
                location_label=location_labels["brunswick"],
                note="Open build session for the tool library shelving.",
                role_requirements=[{"label": "Builder", "required_count": 4}],
                location_id=location_ids["brunswick"],
            )
            print(f"CREATED project_activities title={productive_activity_title}")
        else:
            print(f"SKIPPED project_activities title={productive_activity_title}")

        collective_title = "Fitzroy Repair Collective"
        collective = _find_project_by_title(db, collective_title)
        if collective is None:
            created = create_project(
                db,
                current_user_id=user_id,
                title=collective_title,
                description="Neighbourhood repair collective accepting requests around Fitzroy.",
                project_mode="collective-service",
                project_subtype="standard",
                location_label=location_labels["edinburgh-gardens"],
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_ids["edinburgh-gardens"],
            )["project"]
            collective_id = created["id"]
            collective_slug = str(created["slug"])
            _advance_project_phase(db, collective_id, "phase-5")
            _ensure_collective_requests_enabled(db, collective_id)
            print(f"CREATED projects title={collective_title} phase=phase-5")
        else:
            collective_id = collective["id"]
            collective_slug = str(collective["slug"])
            if collective["current_phase_id"] == "phase-1":
                _advance_project_phase(db, collective_id, "phase-5")
            _ensure_collective_requests_enabled(db, collective_id)
            print(f"SKIPPED projects title={collective_title}")

        collective_activity_title = "Fitzroy Open Repair Shift"
        if _find_activity_by_title(db, collective_id, collective_activity_title) is None:
            create_project_activity(
                db,
                current_user_id=user_id,
                slug=collective_slug,
                title=collective_activity_title,
                scheduled_at=now + timedelta(hours=9),
                ends_at=now + timedelta(hours=12),
                location_label=location_labels["edinburgh-gardens"],
                note="Drop-in repair shift with open helper spots.",
                role_requirements=[{"label": "Repairer", "required_count": 3}],
                location_id=location_ids["edinburgh-gardens"],
            )
            print(f"CREATED project_activities title={collective_activity_title}")
        else:
            print(f"SKIPPED project_activities title={collective_activity_title}")

        personal_title = "Carlton Bike Repair Drop-in"
        personal = _find_project_by_title(db, personal_title)
        if personal is None:
            created = create_project(
                db,
                current_user_id=user_id,
                title=personal_title,
                description="Personal-service bike repair with an open request schedule in Carlton.",
                project_mode="personal-service",
                project_subtype=None,
                location_label=location_labels["carlton-gardens"],
                channel_slugs=[channel_slug],
                community_slugs=[],
                request_mode="both",
                location_id=location_ids["carlton-gardens"],
            )["project"]
            personal_id = created["id"]
            personal_slug = str(created["slug"])
            print(f"CREATED projects title={personal_title}")
        else:
            personal_id = personal["id"]
            personal_slug = str(personal["slug"])
            print(f"SKIPPED projects title={personal_title}")

        personal_activity_title = "Carlton Evening Tune-up"
        if _find_activity_by_title(db, personal_id, personal_activity_title) is None:
            create_project_activity(
                db,
                current_user_id=user_id,
                slug=personal_slug,
                title=personal_activity_title,
                scheduled_at=now + timedelta(hours=10),
                ends_at=now + timedelta(hours=12),
                location_label=location_labels["carlton-gardens"],
                note="Booked tune-up window with one open helper slot.",
                role_requirements=[{"label": "Assistant", "required_count": 1}],
                location_id=location_ids["carlton-gardens"],
            )
            print(f"CREATED project_activities title={personal_activity_title}")
        else:
            print(f"SKIPPED project_activities title={personal_activity_title}")

        # Online/software control sample: no physical map pin expected.
        software_title = "Victoria Coordinating App (online)"
        if _find_project_by_title(db, software_title) is None:
            create_project(
                db,
                current_user_id=user_id,
                title=software_title,
                description="Online software project without a physical location for map contrast.",
                project_mode="productive",
                project_subtype="software",
                location_label="",
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=None,
            )
            print(f"CREATED projects title={software_title} (no map pin)")
        else:
            print(f"SKIPPED projects title={software_title}")

        # Extra Collingwood productive pin without activity (project pin only).
        collingwood_title = "Collingwood Shared Kitchen Fit-out"
        collingwood = _find_project_by_title(db, collingwood_title)
        if collingwood is None:
            created = create_project(
                db,
                current_user_id=user_id,
                title=collingwood_title,
                description="Kitchen fit-out past proposal stage — project pin only for map checks.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_labels["collingwood"],
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_ids["collingwood"],
            )["project"]
            _advance_project_phase(db, created["id"], "phase-5")
            print(f"CREATED projects title={collingwood_title} phase=phase-5")
        else:
            if collingwood["current_phase_id"] == "phase-1":
                _advance_project_phase(db, collingwood["id"], "phase-5")
            print(f"SKIPPED projects title={collingwood_title}")

    print(
        "Victoria map samples ready. "
        f"Login demo user: {VICTORIA_DEMO_USERNAME} / {VICTORIA_DEMO_PASSWORD}. "
        "Center the map on Melbourne and use window=Today or All time."
    )


def _ensure_active_detail_link(
    db: Session,
    *,
    source_kind: str,
    source_id: UUID,
    target_kind: str,
    target_id: UUID,
    relationship_label: str,
    summary: str,
    link_kind: str = "manual",
    status: str = "active",
) -> UUID:
    existing = db.execute(
        select(detail_links.c.id).where(
            detail_links.c.source_kind == source_kind,
            detail_links.c.target_kind == target_kind,
            detail_links.c.source_project_id == (source_id if source_kind == "project" else None),
            detail_links.c.source_event_id == (source_id if source_kind == "event" else None),
            detail_links.c.target_project_id == (target_id if target_kind == "project" else None),
            detail_links.c.target_event_id == (target_id if target_kind == "event" else None),
            detail_links.c.status == status,
        )
    ).first()
    if existing is not None:
        print(f"SKIPPED detail_links {source_kind}->{target_kind} label={relationship_label} status={status}")
        return existing[0]

    link_id = db.execute(
        sa.insert(detail_links)
        .values(
            source_kind=source_kind,
            source_project_id=source_id if source_kind == "project" else None,
            source_event_id=source_id if source_kind == "event" else None,
            target_kind=target_kind,
            target_project_id=target_id if target_kind == "project" else None,
            target_event_id=target_id if target_kind == "event" else None,
            relationship_label=relationship_label,
            summary=summary,
            link_kind=link_kind,
            status=status,
        )
        .returning(detail_links.c.id)
    ).scalar_one()
    db.commit()
    print(f"CREATED detail_links {source_kind}->{target_kind} label={relationship_label} status={status}")
    return link_id


def _ensure_open_detail_link_request(
    db: Session,
    *,
    source_kind: str,
    source_id: UUID,
    target_kind: str,
    target_id: UUID,
    summary: str,
    proposed_by: UUID,
) -> None:
    existing = db.execute(
        select(detail_link_requests.c.id).where(
            detail_link_requests.c.source_kind == source_kind,
            detail_link_requests.c.target_kind == target_kind,
            detail_link_requests.c.source_project_id
            == (source_id if source_kind == "project" else None),
            detail_link_requests.c.source_event_id == (source_id if source_kind == "event" else None),
            detail_link_requests.c.target_project_id
            == (target_id if target_kind == "project" else None),
            detail_link_requests.c.target_event_id == (target_id if target_kind == "event" else None),
            detail_link_requests.c.status == "open",
            detail_link_requests.c.request_type == "create",
        )
    ).first()
    if existing is not None:
        print(f"SKIPPED detail_link_requests {source_kind}->{target_kind}")
        return

    db.execute(
        sa.insert(detail_link_requests).values(
            source_kind=source_kind,
            source_project_id=source_id if source_kind == "project" else None,
            source_event_id=source_id if source_kind == "event" else None,
            target_kind=target_kind,
            target_project_id=target_id if target_kind == "project" else None,
            target_event_id=target_id if target_kind == "event" else None,
            relationship_label="Linked",
            summary=summary,
            request_type="create",
            proposed_by=proposed_by,
            status="open",
        )
    )
    db.commit()
    print(f"CREATED detail_link_requests {source_kind}->{target_kind}")


def _ensure_vote(
    db: Session,
    *,
    request_id: UUID,
    voter_id: UUID,
    vote: str,
    vote_scope: str,
) -> None:
    existing = db.execute(
        select(detail_link_request_votes.c.vote).where(
            detail_link_request_votes.c.request_id == request_id,
            detail_link_request_votes.c.voter_id == voter_id,
            detail_link_request_votes.c.vote_scope == vote_scope,
        )
    ).first()
    if existing is not None:
        return
    db.execute(
        sa.insert(detail_link_request_votes).values(
            request_id=request_id,
            voter_id=voter_id,
            vote=vote,
            vote_scope=vote_scope,
        )
    )


def _ensure_resolved_detail_link_request(
    db: Session,
    *,
    source_kind: str,
    source_id: UUID,
    target_kind: str,
    target_id: UUID,
    summary: str,
    proposed_by: UUID,
    status: str,
    request_type: str = "create",
    link_id: UUID | None = None,
    seed_votes: bool = True,
) -> UUID:
    existing = db.execute(
        select(detail_link_requests.c.id).where(
            detail_link_requests.c.source_kind == source_kind,
            detail_link_requests.c.target_kind == target_kind,
            detail_link_requests.c.source_project_id
            == (source_id if source_kind == "project" else None),
            detail_link_requests.c.source_event_id == (source_id if source_kind == "event" else None),
            detail_link_requests.c.target_project_id
            == (target_id if target_kind == "project" else None),
            detail_link_requests.c.target_event_id == (target_id if target_kind == "event" else None),
            detail_link_requests.c.request_type == request_type,
            detail_link_requests.c.status == status,
        )
    ).first()
    if existing is not None:
        print(
            f"SKIPPED detail_link_requests {request_type}/{status} "
            f"{source_kind}->{target_kind}"
        )
        return existing[0]

    request_id = db.execute(
        sa.insert(detail_link_requests)
        .values(
            source_kind=source_kind,
            source_project_id=source_id if source_kind == "project" else None,
            source_event_id=source_id if source_kind == "event" else None,
            target_kind=target_kind,
            target_project_id=target_id if target_kind == "project" else None,
            target_event_id=target_id if target_kind == "event" else None,
            relationship_label="Linked",
            summary=summary,
            request_type=request_type,
            link_id=link_id,
            proposed_by=proposed_by,
            status=status,
        )
        .returning(detail_link_requests.c.id)
    ).scalar_one()

    if seed_votes:
        source_vote = "no" if status == "rejected" else "yes"
        target_vote = "no" if status == "rejected" else "yes"
        _ensure_vote(
            db,
            request_id=request_id,
            voter_id=proposed_by,
            vote=source_vote,
            vote_scope="source",
        )
        _ensure_vote(
            db,
            request_id=request_id,
            voter_id=proposed_by,
            vote=target_vote,
            vote_scope="target",
        )

    db.commit()
    print(
        f"CREATED detail_link_requests {request_type}/{status} "
        f"{source_kind}->{target_kind}"
    )
    return request_id


def seed_detail_links_demo() -> None:
    """Idempotent linked projects/events for Links tab styling and right-rail votes."""
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    now = datetime.now(UTC)

    with Session(engine) as db:
        user_id = _ensure_demo_user(db)
        channel_slug = _ensure_victoria_channel(db, user_id)
        location_id, location_label = _ensure_location(db, "brunswick")

        hub_title = "Links Demo Hub"
        hub = _find_project_by_title(db, hub_title)
        if hub is None:
            hub = create_project(
                db,
                current_user_id=user_id,
                title=hub_title,
                description="Deterministic hub project for reviewing compact/expandable Links UI.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_id,
            )["project"]
            print(f"CREATED projects title={hub_title}")
        else:
            print(f"SKIPPED projects title={hub_title}")

        partner_title = "Links Demo Partner Workshop"
        partner = _find_project_by_title(db, partner_title)
        if partner is None:
            partner = create_project(
                db,
                current_user_id=user_id,
                title=partner_title,
                description="Partner project used as an active linked record in the Links demo.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_id,
            )["project"]
            print(f"CREATED projects title={partner_title}")
        else:
            print(f"SKIPPED projects title={partner_title}")

        successor_title = "Links Demo Converted Successor"
        successor = _find_project_by_title(db, successor_title)
        if successor is None:
            successor = create_project(
                db,
                current_user_id=user_id,
                title=successor_title,
                description="Successor project showing conversion lineage on the Links tab.",
                project_mode="productive",
                project_subtype="software",
                location_label="",
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=None,
            )["project"]
            print(f"CREATED projects title={successor_title}")
        else:
            print(f"SKIPPED projects title={successor_title}")

        meetup_title = "Links Demo Neighbourhood Meetup"
        meetup = _find_event_by_title(db, meetup_title)
        if meetup is None:
            meetup = create_event(
                db,
                current_user_id=user_id,
                title=meetup_title,
                description="Event linked from the Links Demo Hub for project↔event styling.",
                is_private=False,
                time_label="Next week",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                scheduled_at=now + timedelta(days=3),
                audience="public",
                governance="collaborative",
                location_id=location_id,
            )["event"]
            print(f"CREATED events title={meetup_title}")
        else:
            print(f"SKIPPED events title={meetup_title}")

        pending_target_title = "Links Demo Pending Target"
        pending_target = _find_project_by_title(db, pending_target_title)
        if pending_target is None:
            pending_target = create_project(
                db,
                current_user_id=user_id,
                title=pending_target_title,
                description="Target for an open link vote shown in the right rail.",
                project_mode="collective-service",
                project_subtype="standard",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_id,
            )["project"]
            print(f"CREATED projects title={pending_target_title}")
        else:
            print(f"SKIPPED projects title={pending_target_title}")

        rejected_target_title = "Links Demo Rejected Target"
        rejected_target = _find_project_by_title(db, rejected_target_title)
        if rejected_target is None:
            rejected_target = create_project(
                db,
                current_user_id=user_id,
                title=rejected_target_title,
                description="Target for a rejected create-link vote shown in Past link votes.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_id,
            )["project"]
            print(f"CREATED projects title={rejected_target_title}")
        else:
            print(f"SKIPPED projects title={rejected_target_title}")

        severed_archive_title = "Links Demo Severed Archive"
        severed_archive = _find_project_by_title(db, severed_archive_title)
        if severed_archive is None:
            severed_archive = create_project(
                db,
                current_user_id=user_id,
                title=severed_archive_title,
                description="Inactive historical link used to demo an approved sever vote.",
                project_mode="productive",
                project_subtype="standard",
                location_label=location_label,
                channel_slugs=[channel_slug],
                community_slugs=[],
                location_id=location_id,
            )["project"]
            print(f"CREATED projects title={severed_archive_title}")
        else:
            print(f"SKIPPED projects title={severed_archive_title}")

        hub_id = hub["id"]
        partner_id = partner["id"]
        successor_id = successor["id"]
        meetup_id = meetup["id"]
        pending_target_id = pending_target["id"]
        rejected_target_id = rejected_target["id"]
        severed_archive_id = severed_archive["id"]

        partner_link_id = _ensure_active_detail_link(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=partner_id,
            relationship_label="Linked",
            summary="Shared tooling and volunteers between the hub and partner workshop.",
        )
        _ensure_active_detail_link(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="event",
            target_id=meetup_id,
            relationship_label="Linked",
            summary="The meetup is the public face of the hub's neighbourhood outreach.",
        )
        _ensure_active_detail_link(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=successor_id,
            relationship_label="Converted into",
            summary="Permanent conversion lineage from the hub into its software successor.",
            link_kind="conversion",
        )
        severed_link_id = _ensure_active_detail_link(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=severed_archive_id,
            relationship_label="Linked",
            summary="Former coordination link that members voted to sever.",
            status="inactive",
        )
        _ensure_open_detail_link_request(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=pending_target_id,
            summary="Should these projects coordinate requests and shared helpers?",
            proposed_by=user_id,
        )
        _ensure_resolved_detail_link_request(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=rejected_target_id,
            summary="Members rejected linking these projects after reviewing overlap.",
            proposed_by=user_id,
            status="rejected",
            request_type="create",
        )
        _ensure_resolved_detail_link_request(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=severed_archive_id,
            summary="Both sides approved severing this coordination link.",
            proposed_by=user_id,
            status="approved",
            request_type="sever",
            link_id=severed_link_id,
        )
        _ensure_resolved_detail_link_request(
            db,
            source_kind="project",
            source_id=hub_id,
            target_kind="project",
            target_id=partner_id,
            summary="A sever attempt was rejected; the active link remains.",
            proposed_by=user_id,
            status="rejected",
            request_type="sever",
            link_id=partner_link_id,
        )

    print(
        "Links demo ready. Open project 'Links Demo Hub' → Links tab, "
        "and check the right rail for the open link vote."
    )


def main() -> None:
    seed_platform_channel()
    seed_victoria_map_samples()
    seed_detail_links_demo()


if __name__ == "__main__":
    main()
