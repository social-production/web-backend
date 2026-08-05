from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.locations import (
    create_location,
    enforce_geocoding_rate_limit,
    get_location,
    reverse_geocode,
    search_places,
    serialize_location,
)
from app.services.locations.ip_hint import ip_location_hint

router = APIRouter(prefix="/locations", tags=["locations"])


class LocationResult(BaseModel):
    provider_place_id: str | None = None
    display_label: str
    latitude: float | None = None
    longitude: float | None = None
    region: str | None = None
    country: str | None = None
    precision: str
    is_online: bool


class LocationRecord(LocationResult):
    id: UUID


class LocationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_place_id: str | None = Field(default=None, max_length=160)
    display_label: str = Field(min_length=1, max_length=240)
    latitude: float | None = None
    longitude: float | None = None
    region: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    precision: str = Field(default="approximate", max_length=16)
    is_online: bool = False


class LocationSearchResponse(BaseModel):
    items: list[LocationResult]


class LocationCreateResponse(BaseModel):
    location: LocationRecord


@router.get("/search", response_model=LocationSearchResponse)
async def locations_search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=5, ge=1, le=10),
    country_codes: str | None = Query(default=None, max_length=40),
    viewbox: str | None = Query(default=None, max_length=80),
    _viewer_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    await enforce_geocoding_rate_limit(request)
    parsed_viewbox: tuple[float, float, float, float] | None = None
    if viewbox and viewbox.strip():
        parts = [part.strip() for part in viewbox.split(",")]
        if len(parts) == 4:
            try:
                parsed_viewbox = tuple(float(part) for part in parts)  # type: ignore[assignment]
            except ValueError:
                parsed_viewbox = None
    items = await search_places(
        q,
        limit=limit,
        country_codes=country_codes,
        viewbox=parsed_viewbox,
    )
    return {"items": items}


@router.get("/ip-hint", response_model=LocationSearchResponse)
async def locations_ip_hint(
    request: Request,
    _viewer_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    item = await ip_location_hint(request)
    return {"items": [item]}


@router.get("/reverse", response_model=LocationSearchResponse)
async def locations_reverse(
    request: Request,
    lat: float = Query(),
    lon: float = Query(),
    _viewer_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    await enforce_geocoding_rate_limit(request)
    items = await reverse_geocode(lat, lon)
    return {"items": items}


@router.post("", response_model=LocationCreateResponse)
def locations_create(
    payload: LocationCreateRequest,
    db: Session = Depends(get_db),
    _current_user_id: UUID = Depends(get_current_user_id),
) -> dict[str, object]:
    row = create_location(db, payload.model_dump())
    db.commit()
    serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
    assert serialized is not None
    return {"location": serialized}


@router.get("/{location_id}", response_model=LocationCreateResponse)
def locations_get(
    location_id: UUID,
    db: Session = Depends(get_db),
    _viewer_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    row = get_location(db, location_id)
    serialized = serialize_location(row, viewer_authorized=True, is_private_entity=False)
    if serialized is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    return {"location": serialized}
