from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_cache, get_db
from app.services.events import (
    create_event,
    get_event_by_slug,
    join_event,
    toggle_event_attendance,
    toggle_event_signal,
)

router = APIRouter(prefix="/events", tags=["events"])


class EventCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    is_private: bool = False
    time_label: str = Field(min_length=1, max_length=120)
    location_label: str = Field(min_length=1, max_length=160)
    scheduled_at: datetime | None = None
    channel_slugs: list[str] = Field(default_factory=list)


class EventTagOut(BaseModel):
    id: UUID
    tag_kind: str
    channel_id: UUID | None = None
    community_id: UUID | None = None


class SignalCountsOut(BaseModel):
    demand: int
    opposition: int
    total: int


class EventOut(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str
    created_by: UUID | None = None
    is_private: bool
    current_phase_id: str
    time_label: str
    location_label: str
    scheduled_at: object
    signal_count: int
    vote_count: int
    comment_count: int
    going_count: int
    member_count: int
    created_at: object
    updated_at: object
    last_activity_at: object
    tags: list[EventTagOut]
    signals: SignalCountsOut


class EventResponse(BaseModel):
    event: EventOut


class EventMembershipResponse(BaseModel):
    ok: bool
    joined: bool
    slug: str


class EventAttendanceToggleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    attendance_state: str = Field(pattern="^(going|not-going)$")


class EventAttendanceToggleResponse(BaseModel):
    ok: bool
    slug: str
    action: str
    attendance_state: str | None = None
    going_count: int


class EventSignalToggleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    signal_type: str = Field(pattern="^(demand|opposition)$")


class EventSignalToggleResponse(BaseModel):
    ok: bool
    slug: str
    action: str
    signal_type: str
    signals: SignalCountsOut


@router.post("", response_model=EventResponse)
async def create_new_event(
    payload: EventCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_event(
        db=db,
        current_user_id=current_user_id,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        is_private=payload.is_private,
        time_label=payload.time_label,
        location_label=payload.location_label,
        channel_slugs=payload.channel_slugs,
        scheduled_at=payload.scheduled_at,
    )


@router.get("/{slug}", response_model=EventResponse)
async def get_event(
    slug: str,
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await get_event_by_slug(db=db, cache=cache, slug=slug)


@router.post("/{slug}/join", response_model=EventMembershipResponse)
async def join_event_route(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_event(db=db, current_user_id=current_user_id, slug=slug)


@router.post("/{slug}/attendance", response_model=EventAttendanceToggleResponse)
async def toggle_event_attendance_route(
    slug: str,
    payload: EventAttendanceToggleRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return toggle_event_attendance(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        attendance_state=payload.attendance_state,
    )


@router.post("/{slug}/signals", response_model=EventSignalToggleResponse)
async def toggle_event_signal_route(
    slug: str,
    payload: EventSignalToggleRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await toggle_event_signal(
        db=db,
        cache=cache,
        current_user_id=current_user_id,
        slug=slug,
        signal_type=payload.signal_type,
    )