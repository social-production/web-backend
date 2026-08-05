from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.services.locations.model import create_location, get_location


def resolve_entity_location_fields(
    db: Session,
    *,
    location_id: UUID | None = None,
    location_label: str | None = None,
) -> tuple[UUID | None, str]:
    """Resolve optional location_id and label for entity writes."""
    if location_id is not None:
        row = get_location(db, location_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="location_not_found",
            )
        return location_id, str(row["display_label"])

    label = (location_label or "").strip()
    if not label:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="location_label_required",
        )
    return None, label


def ensure_location_id(
    db: Session,
    *,
    location_id: UUID | None,
    location_label: str | None,
    provider_place_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    region: str | None = None,
    country: str | None = None,
    precision: str = "approximate",
    is_online: bool = False,
) -> tuple[UUID | None, str]:
    """Return persisted location_id and display label for create flows."""
    if location_id is not None:
        return resolve_entity_location_fields(
            db, location_id=location_id, location_label=location_label
        )

    label = (location_label or "").strip()
    if is_online:
        row = create_location(
            db,
            {
                "display_label": label or "Online",
                "is_online": True,
                "precision": precision,
                "provider_place_id": provider_place_id,
                "region": region,
                "country": country,
            },
        )
        return row["id"], str(row["display_label"])

    if latitude is not None and longitude is not None and label:
        row = create_location(
            db,
            {
                "provider_place_id": provider_place_id,
                "display_label": label,
                "latitude": latitude,
                "longitude": longitude,
                "region": region,
                "country": country,
                "precision": precision,
                "is_online": False,
            },
        )
        return row["id"], str(row["display_label"])

    return resolve_entity_location_fields(db, location_label=label)
