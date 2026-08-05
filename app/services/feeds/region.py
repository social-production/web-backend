"""Regional feed and map-marker discovery helpers."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, func, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_memberships,
    events,
    help_requests,
    locations,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_service_request_settings,
    projects,
    scope_memberships,
)
from app.services.content import _help_request_role_summaries, _load_help_request_roles
from app.services.feeds.builder import _build_feed
from app.services.feeds.location_display import apply_feed_location_labels
from app.services.feeds.ranking import (
    apply_schedule_window_filter,
    calendar_window_end,
    calendar_window_start,
    filter_entity_type,
    normalize_filter,
    normalize_sort,
    normalize_window,
    resolve_display_timezone,
    sort_order_columns,
)
from app.services.feeds.selects import (
    _events_select,
    _help_requests_select,
    _projects_select,
)
from app.services.feeds.serializers import (
    _fetch_active_reports,
    _fetch_active_votes_for_rows,
    _fetch_latest_updates_for_items,
    _fetch_tags_for_items,
    _fetch_viewer_signals_for_rows,
    _serialize_item,
)
from app.services.locations.model import has_confirmed_physical_location, serialize_location

REGION_RADII_KM = frozenset({10, 25, 50, 100})
DEFAULT_RADIUS_KM = 25
MAX_RADIUS_KM = 20_000
EARTH_RADIUS_KM = 6371.0


def validate_region_center(lat: float, lon: float) -> tuple[float, float]:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_region_coordinates",
        ) from exc
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="region_coordinates_out_of_range",
        )
    return latitude, longitude


def normalize_radius_km(radius_km: int | float | None) -> int:
    try:
        value = int(radius_km if radius_km is not None else DEFAULT_RADIUS_KM)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_region_radius",
        ) from exc
    if value < 1 or value > MAX_RADIUS_KM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"radius_km must be between 1 and {MAX_RADIUS_KM}",
        )
    return value


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    lat_delta = radius_km / 111.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    lon_delta = radius_km / (111.0 * cos_lat)
    return (
        max(-90.0, lat - lat_delta),
        min(90.0, lat + lat_delta),
        max(-180.0, lon - lon_delta),
        min(180.0, lon + lon_delta),
    )


def _location_distance_expr(lat: float, lon: float):
    """Approximate great-circle distance in SQL (km) for ordering/filter assist."""
    return (
        EARTH_RADIUS_KM
        * 2
        * func.asin(
            func.sqrt(
                func.pow(func.sin(func.radians(locations.c.latitude - lat) / 2), 2)
                + func.cos(func.radians(lat))
                * func.cos(func.radians(locations.c.latitude))
                * func.pow(func.sin(func.radians(locations.c.longitude - lon) / 2), 2)
            )
        )
    )


def _physical_location_join(entity_location_id):
    return and_(
        locations.c.id == entity_location_id,
        locations.c.is_online.is_(False),
        locations.c.latitude.is_not(None),
        locations.c.longitude.is_not(None),
    )


def get_region_feed(
    db: Session,
    *,
    lat: float,
    lon: float,
    radius_km: int = DEFAULT_RADIUS_KM,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
    window: str = "all",
    entity_filter: str = "all",
    include_online: bool = False,
    timezone_name: str | None = None,
) -> dict[str, object]:
    center_lat, center_lon = validate_region_center(lat, lon)
    safe_radius = normalize_radius_km(radius_km)
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    min_lat, max_lat, min_lon, max_lon = bounding_box(center_lat, center_lon, safe_radius)

    selects_by_type = {
        "project": _projects_select(None, None, public_only=True),
        "thread": None,  # threads are not map/region physical entities
        "event": _events_select(None, None, public_only=True),
        "help_request": _help_requests_select(None, None, public_only=True),
    }
    if not include_online:
        # Prefer entities with confirmed physical locations for the region list.
        pass

    wanted_type = filter_entity_type(safe_filter)
    if wanted_type == "thread":
        return {
            "total": 0,
            "sort": safe_sort,
            "window": safe_window,
            "filter": safe_filter,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "radius_km": safe_radius,
            "center": {"latitude": center_lat, "longitude": center_lon},
            "include_online": include_online,
            "items": [],
        }

    parts = []
    for entity_type, base in selects_by_type.items():
        if base is None:
            continue
        if wanted_type is not None and entity_type != wanted_type:
            continue
        if entity_type == "project":
            q = base.join(locations, _physical_location_join(projects.c.location_id))
        elif entity_type == "event":
            q = base.where(events.c.is_private.is_(False)).join(
                locations, _physical_location_join(events.c.location_id)
            )
        else:
            q = base.join(locations, _physical_location_join(help_requests.c.location_id))
        q = q.where(
            locations.c.latitude.between(min_lat, max_lat),
            locations.c.longitude.between(min_lon, max_lon),
        )
        parts.append(q)

    online_items: list[dict[str, Any]] = []
    if include_online and wanted_type in {None, "event", "help_request", "project"}:
        online_items = _online_region_items(
            db,
            safe_window=safe_window,
            entity_filter=safe_filter,
            current_user_id=current_user_id,
            limit=bounded_limit,
            timezone_name=timezone_name,
        )

    if not parts:
        return {
            "total": len(online_items),
            "sort": safe_sort,
            "window": safe_window,
            "filter": safe_filter,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "radius_km": safe_radius,
            "center": {"latitude": center_lat, "longitude": center_lon},
            "include_online": include_online,
            "items": online_items[:bounded_limit],
        }

    combined = union_all(*parts).subquery("region_feed")
    stmt = select(combined)
    stmt = apply_schedule_window_filter(stmt, combined, safe_window, timezone_name)
    # Fetch a wider page then distance-filter in Python for accurate haversine.
    stmt = stmt.order_by(*sort_order_columns(combined, safe_sort)).limit(300)
    rows = db.execute(stmt).mappings().all()

    enriched: list[dict[str, Any]] = []
    location_ids = set()
    for row in rows:
        # Resolve location via a second query keyed by entity — cheaper than altering selects.
        location_ids.add(str(row["id"]))

    loc_by_entity = _load_entity_locations(db, rows)
    for row in rows:
        loc = loc_by_entity.get(str(row["id"]))
        if loc is None or not has_confirmed_physical_location(loc):
            continue
        lat_v = float(loc["latitude"])
        lon_v = float(loc["longitude"])
        distance = haversine_km(center_lat, center_lon, lat_v, lon_v)
        if distance > safe_radius:
            continue
        enriched.append({**dict(row), "_distance_km": round(distance, 2)})

    if safe_filter in {"all", "projects"}:
        enriched.extend(
            _region_project_activity_feed_items(
                db,
                center_lat=center_lat,
                center_lon=center_lon,
                safe_radius=safe_radius,
                safe_window=safe_window,
                timezone_name=timezone_name,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
            )
        )

    enriched.sort(
        key=lambda item: (
            item["_distance_km"],
            str(item.get("scheduled_at") or item.get("last_activity_at") or ""),
        )
    )
    page = enriched[bounded_offset : bounded_offset + bounded_limit]

    project_ids = [row["id"] for row in page if row["entity_type"] == "project"]
    event_ids = [row["id"] for row in page if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in page if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, [], event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, page, current_user_id)
    viewer_signals = _fetch_viewer_signals_for_rows(db, page, current_user_id)
    active_reports = _fetch_active_reports(db, page, current_user_id)

    items = []
    for row in page:
        if row["entity_type"] == "project_activity":
            items.append(_serialize_region_project_activity(row))
            continue
        serialized = _serialize_item(
            row,
            tags,
            active_votes,
            updates,
            help_request_roles=None,
            viewer_signals=viewer_signals,
            active_reports=active_reports,
        )
        serialized["distance_km"] = row["_distance_km"]
        serialized["is_online"] = False
        items.append(serialized)

    items = apply_feed_location_labels(db, items)

    if include_online:
        # Online list is appended (never mixed into distance ranking as pins).
        items.extend(online_items)

    return {
        "total": len(enriched) + (len(online_items) if include_online else 0),
        "sort": safe_sort,
        "window": safe_window,
        "filter": safe_filter,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "radius_km": safe_radius,
        "center": {"latitude": center_lat, "longitude": center_lon},
        "include_online": include_online,
        "items": items[:bounded_limit] if not include_online else items,
    }


def _region_project_activity_feed_items(
    db: Session,
    *,
    center_lat: float,
    center_lon: float,
    safe_radius: int,
    safe_window: str,
    timezone_name: str | None,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
) -> list[dict[str, Any]]:
    """Scheduled project activities with physical locations for the regional feed."""
    viewer_tz = resolve_display_timezone(timezone_name)
    viewer_now = datetime.now(viewer_tz)
    activity_commitment_summaries: dict[str, tuple[int, int]] = {}
    items: list[dict[str, Any]] = []

    def _is_upcoming(ts: datetime | None) -> bool:
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(viewer_tz) >= viewer_now

    def _within_window(ts: datetime | None) -> bool:
        if ts is None:
            return safe_window == "all"
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if safe_window in {"all", "custom"}:
            return True
        window_start = calendar_window_start(safe_window, timezone_name)
        if window_start is None:
            return True
        if ts < window_start:
            return False
        window_end = calendar_window_end(safe_window, timezone_name)
        if window_end is not None and ts >= window_end:
            return False
        return True

    project_activity_rows = (
        db.execute(
            select(
                project_activities.c.id,
                project_activities.c.title,
                project_activities.c.scheduled_at,
                project_activities.c.ends_at,
                projects.c.slug.label("project_slug"),
                projects.c.title.label("parent_title"),
                projects.c.project_mode,
                locations,
            )
            .select_from(
                project_activities.join(
                    projects, projects.c.id == project_activities.c.project_id
                ).join(locations, _physical_location_join(project_activities.c.location_id))
            )
            .where(
                projects.c.is_closed.is_(False),
                projects.c.moderation_state != "removed",
                project_activities.c.scheduled_at.is_not(None),
                locations.c.latitude.between(min_lat, max_lat),
                locations.c.longitude.between(min_lon, max_lon),
            )
            .limit(200)
        )
        .mappings()
        .all()
    )

    for row in project_activity_rows:
        scheduled_at = row["scheduled_at"]
        if (
            scheduled_at is None
            or not _within_window(scheduled_at)
            or not _is_upcoming(scheduled_at)
        ):
            continue
        serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
        if serialized is None or serialized["latitude"] is None:
            continue
        distance = haversine_km(
            center_lat,
            center_lon,
            float(serialized["latitude"]),
            float(serialized["longitude"]),
        )
        if distance > safe_radius:
            continue
        activity_id = str(row["id"])
        if activity_id not in activity_commitment_summaries:
            signup_rows = db.execute(
                select(func.count(project_activity_assignments.c.user_id))
                .select_from(
                    project_activity_roles.outerjoin(
                        project_activity_assignments,
                        project_activity_assignments.c.role_id == project_activity_roles.c.id,
                    )
                )
                .where(project_activity_roles.c.activity_id == row["id"])
            ).scalar_one()
            minimum_rows = db.execute(
                select(func.coalesce(func.sum(project_activity_roles.c.required_count), 0)).where(
                    project_activity_roles.c.activity_id == row["id"]
                )
            ).scalar_one()
            activity_commitment_summaries[activity_id] = (
                int(signup_rows or 0),
                int(minimum_rows or 0),
            )
        committed_count, minimum_participants = activity_commitment_summaries[activity_id]
        if (
            row["project_mode"] == "personal-service"
            and minimum_participants > 0
            and committed_count >= minimum_participants
        ):
            continue
        scheduled_iso = (
            scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else str(scheduled_at)
        )
        items.append(
            {
                "entity_type": "project_activity",
                "id": activity_id,
                "slug": row["project_slug"],
                "title": row["title"],
                "body": row["parent_title"] or "",
                "parent_title": row["parent_title"],
                "project_mode": row["project_mode"],
                "scheduled_at": scheduled_iso,
                "ends_at": row["ends_at"].isoformat()
                if isinstance(row["ends_at"], datetime)
                else row["ends_at"],
                "location_label": serialized["display_label"],
                "created_at": scheduled_iso,
                "last_activity_at": scheduled_iso,
                "_distance_km": round(distance, 2),
            }
        )
    return items


def _serialize_region_project_activity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "entity_type": "project_activity",
        "slug": row["slug"],
        "title": row["title"],
        "body": row.get("body") or "",
        "audience": None,
        "author_id": None,
        "author_username": None,
        "signal_count": 0,
        "support_count": 0,
        "oppose_count": 0,
        "favorability": None,
        "viewer_signal": None,
        "vote_count": 0,
        "comment_count": 0,
        "member_count": 0,
        "going_count": 0,
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "project_mode": row["project_mode"],
        "project_subtype": None,
        "stage_label": None,
        "current_phase_id": None,
        "location_label": row.get("location_label") or "",
        "is_private": False,
        "scheduled_at": row.get("scheduled_at"),
        "time_label": None,
        "active_vote": 0,
        "channel_tags": [],
        "community_tags": [],
        "parent_title": row.get("parent_title"),
        "ends_at": row.get("ends_at"),
        "distance_km": row["_distance_km"],
        "is_online": False,
    }


def _load_entity_locations(db: Session, rows: list[Any]) -> dict[str, Any]:
    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    result: dict[str, Any] = {}

    def _attach(entity_table, ids: list[UUID], id_col, entity_type: str) -> None:
        if not ids:
            return
        joined = (
            db.execute(
                select(id_col.label("entity_id"), locations)
                .select_from(
                    entity_table.join(locations, locations.c.id == entity_table.c.location_id)
                )
                .where(id_col.in_(ids))
            )
            .mappings()
            .all()
        )
        for row in joined:
            result[str(row["entity_id"])] = row

    _attach(projects, project_ids, projects.c.id, "project")
    _attach(events, event_ids, events.c.id, "event")
    _attach(help_requests, help_ids, help_requests.c.id, "help_request")
    return result


def _online_region_items(
    db: Session,
    *,
    safe_window: str,
    entity_filter: str,
    current_user_id: UUID | None,
    limit: int,
    timezone_name: str | None = None,
) -> list[dict[str, Any]]:
    """Online-only entities for the Online list — never include map pins."""
    feed = _build_feed(
        db,
        "recent",
        limit * 3,
        0,
        current_user_id=current_user_id,
        public_only=True,
        window=safe_window if safe_window != "custom" else "all",
        entity_filter=entity_filter if entity_filter != "threads" else "all",
        timezone_name=timezone_name,
    )

    online_ids: set[tuple[str, str]] = set()
    type_map = {
        "events": [("event", events)],
        "projects": [("project", projects)],
        "help_requests": [("help_request", help_requests)],
        "all": [
            ("event", events),
            ("project", projects),
            ("help_request", help_requests),
        ],
    }
    for entity_type, table in type_map.get(entity_filter, []):
        rows = (
            db.execute(
                select(table.c.id)
                .select_from(table.join(locations, locations.c.id == table.c.location_id))
                .where(locations.c.is_online.is_(True))
            )
            .scalars()
            .all()
        )
        for row_id in rows:
            online_ids.add((entity_type, str(row_id)))

    online: list[dict[str, Any]] = []
    for item in feed["items"]:
        entity_type = str(item.get("entity_type") or "")
        entity_id = str(item.get("id") or "")
        label = str(item.get("location_label") or "").strip().lower()
        is_online = (entity_type, entity_id) in online_ids or label in {
            "online",
            "remote",
            "virtual",
        }
        if not is_online:
            continue
        item = dict(item)
        item["distance_km"] = None
        item["is_online"] = True
        online.append(item)
        if len(online) >= limit:
            break
    return online


def _parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        if len(raw) == 10:
            dt = datetime.fromisoformat(raw).replace(tzinfo=UTC)
            if end_of_day:
                return dt + timedelta(days=1) - timedelta(microseconds=1)
            return dt
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_date_filter",
        ) from exc


def _effective_ends_at(
    scheduled_at: datetime | None,
    ends_at: datetime | None,
) -> datetime | None:
    if scheduled_at is None:
        return None
    if ends_at is not None:
        return ends_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    return scheduled_at + timedelta(hours=2)


def _activity_commitment_summary(
    db: Session,
    activity_id: UUID,
    *,
    roles_table,
    assignments_table,
    cache: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    activity_key = str(activity_id)
    if activity_key in cache:
        return cache[activity_key]
    signup_rows = db.execute(
        select(func.count(assignments_table.c.user_id))
        .select_from(
            roles_table.outerjoin(
                assignments_table,
                assignments_table.c.role_id == roles_table.c.id,
            )
        )
        .where(roles_table.c.activity_id == activity_id)
    ).scalar_one()
    minimum_rows = db.execute(
        select(func.coalesce(func.sum(roles_table.c.required_count), 0)).where(
            roles_table.c.activity_id == activity_id
        )
    ).scalar_one()
    summary = (int(signup_rows or 0), int(minimum_rows or 0))
    cache[activity_key] = summary
    return summary


def _request_system_enabled_by_project(
    db: Session,
    project_rows: list[dict[str, Any]],
) -> dict[str, bool]:
    project_ids = [row["id"] for row in project_rows]
    if not project_ids:
        return {}
    settings_rows = (
        db.execute(
            select(
                project_service_request_settings.c.project_id,
                project_service_request_settings.c.enabled,
            ).where(project_service_request_settings.c.project_id.in_(project_ids))
        )
        .mappings()
        .all()
    )
    enabled_by_id = {str(row["project_id"]): bool(row["enabled"]) for row in settings_rows}
    result: dict[str, bool] = {}
    for row in project_rows:
        project_id = str(row["id"])
        mode = row["project_mode"]
        if mode == "personal-service":
            result[project_id] = enabled_by_id.get(project_id, True)
        elif mode == "collective-service":
            result[project_id] = enabled_by_id.get(project_id, False)
        else:
            result[project_id] = False
    return result


def _project_ids_with_open_spot_activities(
    db: Session,
    project_ids: list[UUID],
    *,
    viewer_tz,
    viewer_now: datetime,
    upcoming_only: bool,
) -> set[str]:
    if not project_ids:
        return set()
    activity_rows = (
        db.execute(
            select(
                project_activities.c.id,
                project_activities.c.project_id,
                project_activities.c.scheduled_at,
            ).where(
                project_activities.c.project_id.in_(project_ids),
                project_activities.c.scheduled_at.is_not(None),
            )
        )
        .mappings()
        .all()
    )
    commitment_cache: dict[str, tuple[int, int]] = {}
    open_project_ids: set[str] = set()
    for row in activity_rows:
        scheduled_at = row["scheduled_at"]
        if scheduled_at is not None:
            ts = scheduled_at if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=UTC)
            if upcoming_only and ts.astimezone(viewer_tz) < viewer_now:
                continue
        committed_count, minimum_participants = _activity_commitment_summary(
            db,
            row["id"],
            roles_table=project_activity_roles,
            assignments_table=project_activity_assignments,
            cache=commitment_cache,
        )
        if minimum_participants > 0 and committed_count < minimum_participants:
            open_project_ids.add(str(row["project_id"]))
    return open_project_ids


def _project_pin_eligible(
    project_mode: str,
    *,
    request_enabled: bool,
    has_open_spots: bool,
) -> bool:
    if project_mode == "productive":
        return True
    if project_mode == "personal-service":
        return request_enabled
    if project_mode == "collective-service":
        return request_enabled or has_open_spots
    return False


def _project_pin_subtitle(
    project_mode: str,
    *,
    stage_label: str | None,
    request_enabled: bool,
    has_open_spots: bool,
) -> str:
    if project_mode == "productive":
        return (stage_label or "").strip() or "Proposal"
    if request_enabled:
        return "Accepting requests"
    if has_open_spots:
        return "Open spots"
    return (stage_label or "").strip()


def _event_map_visibility_clause(viewer_user_id: UUID | None):
    """Public events for everyone; private events only for authorized viewers."""
    public = events.c.is_private.is_(False)
    if viewer_user_id is None:
        return public

    is_creator = events.c.created_by == viewer_user_id
    is_event_member = exists(
        select(1)
        .select_from(event_memberships)
        .where(
            event_memberships.c.event_id == events.c.id,
            event_memberships.c.user_id == viewer_user_id,
        )
    )
    is_home_community_member = and_(
        events.c.audience == "private_community",
        events.c.home_community_id.is_not(None),
        exists(
            select(1)
            .select_from(scope_memberships)
            .where(
                scope_memberships.c.scope_kind == "community",
                scope_memberships.c.scope_id == events.c.home_community_id,
                scope_memberships.c.user_id == viewer_user_id,
            )
        ),
    )
    return or_(public, is_creator, is_event_member, is_home_community_member)


def get_map_markers(
    db: Session,
    *,
    lat: float,
    lon: float,
    radius_km: int = DEFAULT_RADIUS_KM,
    entity_filter: str = "all",
    window: str = "all",
    date_from: str | None = None,
    date_to: str | None = None,
    upcoming_only: bool = True,
    current_user_id: UUID | None = None,
    limit: int = 200,
    timezone_name: str | None = None,
    distance_from_lat: float | None = None,
    distance_from_lon: float | None = None,
) -> dict[str, object]:
    """Authorized, map-eligible markers only. Private coords never leak."""
    viewer_user_id = current_user_id
    center_lat, center_lon = validate_region_center(lat, lon)
    distance_anchor_lat, distance_anchor_lon = (
        validate_region_center(distance_from_lat, distance_from_lon)
        if distance_from_lat is not None and distance_from_lon is not None
        else (center_lat, center_lon)
    )
    safe_radius = normalize_radius_km(radius_km)
    safe_filter = normalize_filter(entity_filter)
    safe_window = normalize_window(window)
    start = _parse_date(date_from)
    end = _parse_date(date_to, end_of_day=True)
    min_lat, max_lat, min_lon, max_lon = bounding_box(center_lat, center_lon, safe_radius)
    bounded_limit = max(1, min(limit, 500))

    markers: list[dict[str, Any]] = []
    viewer_tz = resolve_display_timezone(timezone_name)
    viewer_now = datetime.now(viewer_tz)
    help_role_summaries: dict[str, tuple[int, int]] = {}
    activity_commitment_summaries: dict[str, tuple[int, int]] = {}

    def _is_upcoming(ts: datetime | None) -> bool:
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(viewer_tz) >= viewer_now

    def _within_window(ts: datetime | None) -> bool:
        if ts is None:
            return safe_window == "all" and start is None and end is None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if start and ts < start:
            return False
        if end and ts > end:
            return False
        if safe_window in {"all", "custom"}:
            return True
        window_start = calendar_window_start(safe_window, timezone_name)
        if window_start is None:
            return True
        if ts < window_start:
            return False
        window_end = calendar_window_end(safe_window, timezone_name)
        if window_end is not None and ts >= window_end:
            return False
        return True

    def _include_item(ts: datetime | None, *, enforce_upcoming: bool) -> bool:
        if not _within_window(ts):
            return False
        if enforce_upcoming and upcoming_only and ts is not None and not _is_upcoming(ts):
            return False
        return True

    def _distance_from_anchor(marker_lat: float, marker_lon: float) -> float:
        return haversine_km(distance_anchor_lat, distance_anchor_lon, marker_lat, marker_lon)

    anchor_differs_from_query = (
        distance_from_lat is not None
        and distance_from_lon is not None
        and (
            abs(distance_anchor_lat - center_lat) > 0.001
            or abs(distance_anchor_lon - center_lon) > 0.001
        )
    )

    def _within_query_radius(distance: float) -> bool:
        if anchor_differs_from_query:
            return True
        return distance <= safe_radius

    # Events with confirmed physical location (public + authorized private).
    if safe_filter in {"all", "events"}:
        event_visibility = _event_map_visibility_clause(viewer_user_id)
        event_rows = (
            db.execute(
                select(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.scheduled_at,
                    events.c.ends_at,
                    events.c.last_activity_at,
                    events.c.is_private,
                    locations,
                )
                .select_from(events.join(locations, _physical_location_join(events.c.location_id)))
                .where(
                    event_visibility,
                    events.c.moderation_state != "removed",
                    locations.c.latitude.between(min_lat, max_lat),
                    locations.c.longitude.between(min_lon, max_lon),
                )
                .limit(bounded_limit)
            )
            .mappings()
            .all()
        )
        for row in event_rows:
            if row["scheduled_at"] is None:
                continue
            if not _include_item(row["scheduled_at"], enforce_upcoming=True):
                continue
            serialized = serialize_location(
                row,
                viewer_authorized=True,
                is_private_entity=bool(row["is_private"]),
            )
            if serialized is None or serialized["latitude"] is None:
                continue
            distance = _distance_from_anchor(
                float(serialized["latitude"]), float(serialized["longitude"])
            )
            if not _within_query_radius(distance):
                continue
            markers.append(
                {
                    "id": str(row["id"]),
                    "entity_type": "event",
                    "slug": row["slug"],
                    "title": row["title"],
                    "parent_title": None,
                    "subtitle": None,
                    "href": f"/events/{row['slug']}",
                    "latitude": serialized["latitude"],
                    "longitude": serialized["longitude"],
                    "precision": serialized["precision"],
                    "display_label": serialized["display_label"],
                    "distance_km": round(distance, 2),
                    "scheduled_at": row["scheduled_at"],
                    "ends_at": _effective_ends_at(row["scheduled_at"], row["ends_at"]),
                }
            )

    if safe_filter in {"all", "help_requests"}:
        help_rows = (
            db.execute(
                select(
                    help_requests.c.id,
                    help_requests.c.title,
                    help_requests.c.needed_at,
                    help_requests.c.ends_at,
                    help_requests.c.created_at,
                    locations,
                )
                .select_from(
                    help_requests.join(
                        locations, _physical_location_join(help_requests.c.location_id)
                    )
                )
                .where(
                    help_requests.c.moderation_state != "removed",
                    locations.c.latitude.between(min_lat, max_lat),
                    locations.c.longitude.between(min_lon, max_lon),
                )
                .limit(bounded_limit)
            )
            .mappings()
            .all()
        )
        for row in help_rows:
            if row["needed_at"] is None:
                continue
            if not _include_item(row["needed_at"], enforce_upcoming=True):
                continue
            serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
            if serialized is None or serialized["latitude"] is None:
                continue
            distance = _distance_from_anchor(
                float(serialized["latitude"]), float(serialized["longitude"])
            )
            if not _within_query_radius(distance):
                continue
            help_id = str(row["id"])
            if help_id not in help_role_summaries:
                roles = _load_help_request_roles(db, [row["id"]], viewer_user_id).get(help_id, [])
                help_role_summaries[help_id] = _help_request_role_summaries(roles)
            signup_count, slots_needed = help_role_summaries[help_id]
            markers.append(
                {
                    "id": help_id,
                    "entity_type": "help_request",
                    "slug": None,
                    "title": row["title"],
                    "parent_title": None,
                    "subtitle": None,
                    "href": f"/help-requests/{help_id}",
                    "latitude": serialized["latitude"],
                    "longitude": serialized["longitude"],
                    "precision": serialized["precision"],
                    "display_label": serialized["display_label"],
                    "distance_km": round(distance, 2),
                    "scheduled_at": row["needed_at"],
                    "ends_at": _effective_ends_at(row["needed_at"], row["ends_at"]),
                    "signup_count": signup_count,
                    "slots_needed": slots_needed,
                    "committed_count": None,
                    "minimum_participants": None,
                }
            )

    # Scheduled activities with confirmed physical locations.
    if safe_filter in {"all", "events"}:
        event_visibility = _event_map_visibility_clause(viewer_user_id)
        activity_rows = (
            db.execute(
                select(
                    event_activities.c.id,
                    event_activities.c.title,
                    event_activities.c.scheduled_at,
                    event_activities.c.ends_at,
                    events.c.id.label("parent_id"),
                    events.c.slug.label("event_slug"),
                    events.c.title.label("parent_title"),
                    events.c.is_private,
                    locations,
                )
                .select_from(
                    event_activities.join(events, events.c.id == event_activities.c.event_id).join(
                        locations, _physical_location_join(event_activities.c.location_id)
                    )
                )
                .where(
                    event_visibility,
                    event_activities.c.scheduled_at.is_not(None),
                    locations.c.latitude.between(min_lat, max_lat),
                    locations.c.longitude.between(min_lon, max_lon),
                )
                .limit(bounded_limit)
            )
            .mappings()
            .all()
        )
        for row in activity_rows:
            if not _include_item(row["scheduled_at"], enforce_upcoming=True):
                continue
            serialized = serialize_location(
                row,
                viewer_authorized=True,
                is_private_entity=bool(row["is_private"]),
            )
            if serialized is None or serialized["latitude"] is None:
                continue
            distance = _distance_from_anchor(
                float(serialized["latitude"]), float(serialized["longitude"])
            )
            if not _within_query_radius(distance):
                continue
            activity_id = str(row["id"])
            if activity_id not in activity_commitment_summaries:
                signup_rows = db.execute(
                    select(func.count(event_activity_assignments.c.user_id))
                    .select_from(
                        event_activity_roles.outerjoin(
                            event_activity_assignments,
                            event_activity_assignments.c.role_id == event_activity_roles.c.id,
                        )
                    )
                    .where(event_activity_roles.c.activity_id == row["id"])
                ).scalar_one()
                minimum_rows = db.execute(
                    select(func.coalesce(func.sum(event_activity_roles.c.required_count), 0)).where(
                        event_activity_roles.c.activity_id == row["id"]
                    )
                ).scalar_one()
                activity_commitment_summaries[activity_id] = (
                    int(signup_rows or 0),
                    int(minimum_rows or 0),
                )
            committed_count, minimum_participants = activity_commitment_summaries[activity_id]
            markers.append(
                {
                    "id": activity_id,
                    "entity_type": "activity",
                    "activity_source": "event",
                    "project_mode": None,
                    "slug": row["event_slug"],
                    "title": row["title"],
                    "parent_id": str(row["parent_id"]),
                    "parent_title": row["parent_title"],
                    "subtitle": row["title"],
                    "href": f"/events/{row['event_slug']}",
                    "latitude": serialized["latitude"],
                    "longitude": serialized["longitude"],
                    "precision": serialized["precision"],
                    "display_label": serialized["display_label"],
                    "distance_km": round(distance, 2),
                    "scheduled_at": row["scheduled_at"],
                    "ends_at": _effective_ends_at(row["scheduled_at"], row["ends_at"]),
                    "signup_count": None,
                    "slots_needed": None,
                    "committed_count": committed_count,
                    "minimum_participants": minimum_participants,
                }
            )

    if safe_filter in {"all", "projects"}:
        project_rows = (
            db.execute(
                select(
                    projects.c.id,
                    projects.c.slug,
                    projects.c.title,
                    projects.c.project_mode,
                    projects.c.stage_label,
                    locations,
                )
                .select_from(
                    projects.join(locations, _physical_location_join(projects.c.location_id))
                )
                .where(
                    projects.c.is_closed.is_(False),
                    projects.c.moderation_state != "removed",
                    locations.c.latitude.between(min_lat, max_lat),
                    locations.c.longitude.between(min_lon, max_lon),
                )
                .limit(bounded_limit)
            )
            .mappings()
            .all()
        )
        request_enabled_by_project = _request_system_enabled_by_project(db, list(project_rows))
        open_spot_project_ids = _project_ids_with_open_spot_activities(
            db,
            [row["id"] for row in project_rows],
            viewer_tz=viewer_tz,
            viewer_now=viewer_now,
            upcoming_only=upcoming_only,
        )
        for row in project_rows:
            project_id = str(row["id"])
            request_enabled = request_enabled_by_project.get(project_id, False)
            has_open_spots = project_id in open_spot_project_ids
            if not _project_pin_eligible(
                row["project_mode"],
                request_enabled=request_enabled,
                has_open_spots=has_open_spots,
            ):
                continue
            serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
            if serialized is None or serialized["latitude"] is None:
                continue
            distance = _distance_from_anchor(
                float(serialized["latitude"]), float(serialized["longitude"])
            )
            if not _within_query_radius(distance):
                continue
            markers.append(
                {
                    "id": project_id,
                    "entity_type": "project",
                    "activity_source": None,
                    "project_mode": row["project_mode"],
                    "slug": row["slug"],
                    "title": row["title"],
                    "parent_title": None,
                    "subtitle": _project_pin_subtitle(
                        row["project_mode"],
                        stage_label=row["stage_label"],
                        request_enabled=request_enabled,
                        has_open_spots=has_open_spots,
                    ),
                    "href": f"/projects/{row['slug']}",
                    "latitude": serialized["latitude"],
                    "longitude": serialized["longitude"],
                    "precision": serialized["precision"],
                    "display_label": serialized["display_label"],
                    "distance_km": round(distance, 2),
                    "scheduled_at": None,
                    "ends_at": None,
                    "signup_count": None,
                    "slots_needed": None,
                    "committed_count": None,
                    "minimum_participants": None,
                }
            )

        project_activity_rows = (
            db.execute(
                select(
                    project_activities.c.id,
                    project_activities.c.title,
                    project_activities.c.scheduled_at,
                    project_activities.c.ends_at,
                    projects.c.id.label("parent_id"),
                    projects.c.slug.label("project_slug"),
                    projects.c.title.label("parent_title"),
                    projects.c.project_mode,
                    locations,
                )
                .select_from(
                    project_activities.join(
                        projects, projects.c.id == project_activities.c.project_id
                    ).join(locations, _physical_location_join(project_activities.c.location_id))
                )
                .where(
                    projects.c.is_closed.is_(False),
                    projects.c.moderation_state != "removed",
                    project_activities.c.scheduled_at.is_not(None),
                    locations.c.latitude.between(min_lat, max_lat),
                    locations.c.longitude.between(min_lon, max_lon),
                )
                .limit(bounded_limit)
            )
            .mappings()
            .all()
        )
        for row in project_activity_rows:
            if not _include_item(row["scheduled_at"], enforce_upcoming=True):
                continue
            serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
            if serialized is None or serialized["latitude"] is None:
                continue
            distance = _distance_from_anchor(
                float(serialized["latitude"]), float(serialized["longitude"])
            )
            if not _within_query_radius(distance):
                continue
            activity_id = str(row["id"])
            committed_count, minimum_participants = _activity_commitment_summary(
                db,
                row["id"],
                roles_table=project_activity_roles,
                assignments_table=project_activity_assignments,
                cache=activity_commitment_summaries,
            )
            if (
                row["project_mode"] in {"personal-service", "collective-service"}
                and minimum_participants > 0
                and committed_count >= minimum_participants
            ):
                continue
            markers.append(
                {
                    "id": activity_id,
                    "entity_type": "activity",
                    "activity_source": "project",
                    "project_mode": row["project_mode"],
                    "slug": row["project_slug"],
                    "title": row["title"],
                    "parent_id": str(row["parent_id"]),
                    "parent_title": row["parent_title"],
                    "subtitle": row["title"],
                    "href": f"/projects/{row['project_slug']}",
                    "latitude": serialized["latitude"],
                    "longitude": serialized["longitude"],
                    "precision": serialized["precision"],
                    "display_label": serialized["display_label"],
                    "distance_km": round(distance, 2),
                    "scheduled_at": row["scheduled_at"],
                    "ends_at": _effective_ends_at(row["scheduled_at"], row["ends_at"]),
                    "signup_count": None,
                    "slots_needed": None,
                    "committed_count": committed_count,
                    "minimum_participants": minimum_participants,
                }
            )

    def _scheduled_sort_key(item: dict[str, object]) -> datetime:
        ts = item.get("scheduled_at")
        if not isinstance(ts, datetime):
            return datetime.max.replace(tzinfo=UTC)
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts

    markers.sort(key=_scheduled_sort_key)
    return {
        "radius_km": safe_radius,
        "center": {"latitude": center_lat, "longitude": center_lon},
        "window": safe_window,
        "filter": safe_filter,
        "total": len(markers[:bounded_limit]),
        "items": markers[:bounded_limit],
    }
