from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.feeds import (
    get_home_feed,
    get_map_markers,
    get_personal_feed,
    get_public_feed,
    get_region_feed,
    get_scope_feed,
    get_user_feed,
)

router = APIRouter(prefix="/feeds", tags=["feeds"])

_SORT_PATTERN = "^(trending|recent|popular|oldest|top)$"
_WINDOW_PATTERN = "^(today|week|month|all|custom)$"
_FILTER_PATTERN = "^(all|projects|threads|events|help_requests)$"


class TagRefOut(BaseModel):
    slug: str
    label: str
    kind: str


class HelpRequestRoleFeedOut(BaseModel):
    role_id: UUID | None = None
    title: str
    description: str = ""
    slots: int
    filled_count: int = 0
    is_viewer_assigned: bool = False


class FeedItemOut(BaseModel):
    id: UUID
    entity_type: str
    slug: str | None = None
    title: str
    body: str
    audience: str | None = None
    author_id: UUID | None = None
    author_username: str | None = None
    author_profile_image_url: str | None = None
    signal_count: int
    support_count: int = 0
    oppose_count: int = 0
    favorability: float | None = None
    viewer_signal: str | None = None
    vote_count: int
    comment_count: int
    member_count: int
    going_count: int
    last_activity_at: object
    created_at: object
    project_mode: str | None = None
    project_subtype: str | None = None
    stage_label: str | None = None
    current_phase_id: str | None = None
    location_label: str | None = None
    is_private: bool = False
    scheduled_at: object = None
    time_label: str | None = None
    active_vote: int = 0
    channel_tags: list[TagRefOut] = Field(default_factory=list)
    community_tags: list[TagRefOut] = Field(default_factory=list)
    last_update_at: object = None
    latest_update_body: str | None = None
    feed_source: str | None = None
    roles: list[HelpRequestRoleFeedOut] | None = None
    signup_count: int | None = None
    slots_needed: int | None = None
    distance_km: float | None = None
    is_online: bool | None = None
    moderation_state: str | None = None
    moderation_reason: str | None = None
    is_under_review: bool = False
    has_active_report: bool = False
    report: dict[str, object] | None = None


class FeedResponse(BaseModel):
    total: int
    sort: str
    window: str = "all"
    filter: str = "all"
    limit: int
    offset: int
    items: list[FeedItemOut]
    radius_km: int | None = None
    center: dict[str, float] | None = None
    include_online: bool | None = None


class MapMarkerOut(BaseModel):
    id: str
    entity_type: str
    slug: str | None = None
    title: str
    parent_id: str | None = None
    parent_title: str | None = None
    subtitle: str | None = None
    href: str
    latitude: float
    longitude: float
    precision: str
    display_label: str
    distance_km: float
    scheduled_at: object = None
    ends_at: object = None
    signup_count: int | None = None
    slots_needed: int | None = None
    committed_count: int | None = None
    minimum_participants: int | None = None
    activity_source: str | None = None
    project_mode: str | None = None


class MapMarkersResponse(BaseModel):
    total: int
    radius_km: int
    center: dict[str, float]
    window: str
    filter: str
    items: list[MapMarkerOut]


@router.get("/public", response_model=FeedResponse)
def public_feed(
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_public_feed(
        db=db,
        sort=sort,
        limit=limit,
        offset=offset,
        current_user_id=current_user_id,
        window=window,
        entity_filter=filter,
    )


@router.get("/home", response_model=FeedResponse)
def home_feed(
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_home_feed(
        db=db,
        current_user_id=current_user_id,
        sort=sort,
        limit=limit,
        offset=offset,
        window=window,
        entity_filter=filter,
    )


@router.get("/personal", response_model=FeedResponse)
def personal_feed(
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    scope: str = Query(default="following"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_personal_feed(
        db=db,
        current_user_id=current_user_id,
        sort=sort,
        limit=limit,
        offset=offset,
        scope=scope,
        window=window,
        entity_filter=filter,
    )


@router.get("/scope", response_model=FeedResponse)
def scope_feed(
    kind: str = Query(pattern="^(channel|community)$"),
    slug: str = Query(min_length=1),
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_scope_feed(
        db=db,
        scope_kind=kind,
        slug=slug,
        sort=sort,
        limit=limit,
        offset=offset,
        current_user_id=current_user_id,
        window=window,
        entity_filter=filter,
    )


@router.get("/user/{username}", response_model=FeedResponse)
def user_feed(
    username: str,
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    viewer_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_user_feed(
        db=db,
        username=username,
        viewer_user_id=viewer_user_id,
        sort=sort,
        limit=limit,
        offset=offset,
        window=window,
        entity_filter=filter,
    )


@router.get("/region", response_model=FeedResponse)
def region_feed(
    lat: float = Query(),
    lon: float = Query(),
    radius_km: int = Query(default=25),
    sort: str = Query(default="trending", pattern=_SORT_PATTERN),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    include_online: bool = Query(default=False),
    tz: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_region_feed(
        db=db,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        sort=sort,
        limit=limit,
        offset=offset,
        current_user_id=current_user_id,
        window=window,
        entity_filter=filter,
        include_online=include_online,
        timezone_name=tz,
    )


@router.get("/map-markers", response_model=MapMarkersResponse)
def map_markers(
    lat: float = Query(),
    lon: float = Query(),
    radius_km: int = Query(default=25),
    window: str = Query(default="all", pattern=_WINDOW_PATTERN),
    filter: str = Query(default="all", pattern=_FILTER_PATTERN),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    upcoming_only: bool = Query(default=True),
    tz: str | None = Query(default=None, max_length=80),
    distance_from_lat: float | None = Query(default=None),
    distance_from_lon: float | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_map_markers(
        db=db,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        entity_filter=filter,
        window=window,
        date_from=date_from,
        date_to=date_to,
        upcoming_only=upcoming_only,
        current_user_id=current_user_id,
        limit=limit,
        timezone_name=tz,
        distance_from_lat=distance_from_lat,
        distance_from_lon=distance_from_lon,
    )
