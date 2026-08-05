from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.models.locations import locations

LocationPrecision = Literal["exact", "approximate"]
LOCATION_PRECISIONS = frozenset({"exact", "approximate"})
DEFAULT_PRECISION: LocationPrecision = "approximate"
APPROXIMATE_COORD_DECIMALS = 2
EXACT_COORD_DECIMALS = 6


def _as_float(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def validate_location_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a location write payload."""
    is_online = bool(payload.get("is_online", False))
    precision_raw = str(payload.get("precision") or DEFAULT_PRECISION).strip().lower()
    if precision_raw not in LOCATION_PRECISIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_location_precision",
        )
    precision: LocationPrecision = precision_raw  # type: ignore[assignment]

    display_label = str(payload.get("display_label") or "").strip()
    if not display_label:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_display_label_required",
        )
    if len(display_label) > 240:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_display_label_too_long",
        )

    provider_place_id = payload.get("provider_place_id")
    provider_place_id_norm: str | None
    if provider_place_id is None or str(provider_place_id).strip() == "":
        provider_place_id_norm = None
    else:
        provider_place_id_norm = str(provider_place_id).strip()[:160]

    region = payload.get("region")
    region_norm = str(region).strip()[:120] if region is not None and str(region).strip() else None
    country = payload.get("country")
    country_norm = (
        str(country).strip()[:120] if country is not None and str(country).strip() else None
    )

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")

    if is_online:
        return {
            "provider_place_id": provider_place_id_norm,
            "display_label": display_label,
            "latitude": None,
            "longitude": None,
            "region": region_norm,
            "country": country_norm,
            "precision": precision,
            "is_online": True,
        }

    if latitude is None or longitude is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_coordinates_required",
        )

    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_location_coordinates",
        ) from exc

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_coordinates_out_of_range",
        )

    return {
        "provider_place_id": provider_place_id_norm,
        "display_label": display_label,
        "latitude": round(lat, EXACT_COORD_DECIMALS),
        "longitude": round(lon, EXACT_COORD_DECIMALS),
        "region": region_norm,
        "country": country_norm,
        "precision": precision,
        "is_online": False,
    }


def create_location(db: Session, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    values = validate_location_input(payload)
    row = (
        db.execute(
            insert(locations)
            .values(**values)
            .returning(
                locations.c.id,
                locations.c.provider_place_id,
                locations.c.display_label,
                locations.c.latitude,
                locations.c.longitude,
                locations.c.region,
                locations.c.country,
                locations.c.precision,
                locations.c.is_online,
            )
        )
        .mappings()
        .one()
    )
    return row


def get_location(db: Session, location_id: UUID) -> Mapping[str, Any] | None:
    return db.execute(select(locations).where(locations.c.id == location_id)).mappings().first()


def serialize_location(
    row: Mapping[str, Any] | None,
    *,
    viewer_authorized: bool = True,
    is_private_entity: bool = False,
) -> dict[str, Any] | None:
    """Serialize a location for API responses.

    Private-entity locations are omitted unless the viewer is authorized.
    Approximate precision returns coarsened coordinates; exact returns stored precision.
    """
    if row is None:
        return None

    if is_private_entity and not viewer_authorized:
        return None

    precision = str(row["precision"] or DEFAULT_PRECISION)
    is_online = bool(row["is_online"])
    lat = _as_float(row["latitude"])
    lon = _as_float(row["longitude"])

    if not is_online and lat is not None and lon is not None:
        decimals = (
            APPROXIMATE_COORD_DECIMALS if precision == "approximate" else EXACT_COORD_DECIMALS
        )
        lat = round(lat, decimals)
        lon = round(lon, decimals)

    return {
        "id": row["id"],
        "provider_place_id": row["provider_place_id"],
        "display_label": row["display_label"],
        "latitude": lat,
        "longitude": lon,
        "region": row["region"],
        "country": row["country"],
        "precision": precision,
        "is_online": is_online,
    }


def has_confirmed_physical_location(row: Mapping[str, Any] | None) -> bool:
    if row is None:
        return False
    if bool(row["is_online"]):
        return False
    return row["latitude"] is not None and row["longitude"] is not None


def is_map_eligible(
    *,
    entity_kind: Literal[
        "event",
        "help_request",
        "project",
        "event_activity",
        "project_activity",
    ],
    location: Mapping[str, Any] | None,
    scheduled: bool = False,
    leading_plan_has_physical_location: bool = False,
) -> bool:
    """Physical map pin eligibility rules for Phase 5/6.

    - events/help requests: confirmed physical location
    - project/event activities: confirmed location + schedule
    - projects: only once a leading plan supplies a physical location
    """
    if not has_confirmed_physical_location(location):
        return False

    if entity_kind in {"event", "help_request"}:
        return True

    if entity_kind in {"event_activity", "project_activity"}:
        return scheduled

    if entity_kind == "project":
        return leading_plan_has_physical_location

    return False
