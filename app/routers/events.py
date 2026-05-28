from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_cache, get_db
from app.services.events import (
    add_event_value,
    commit_event_activity_role,
    create_event,
    create_event_activity,
    grant_event_editor,
    get_event_detail,
    join_event,
    revoke_event_editor,
    share_event_with_user,
    toggle_event_attendance,
    toggle_event_signal,
    uncommit_event_activity_role,
    vote_event_value_importance,
)
from app.models import event_activity_roles

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


class EventDetailResponse(BaseModel):
    id: str
    slug: str
    createdAt: str
    title: str
    description: str
    isPrivate: bool
    scheduledAt: str | None = None
    channelTags: list[dict[str, Any]]
    communityTags: list[dict[str, Any]]
    createdByUsername: str
    timeLabel: str
    locationLabel: str
    voteCount: int
    activeVote: int
    commentCount: int
    goingCount: int
    memberCount: int
    lastActivityAt: str
    signalSummary: dict[str, Any] | None = None
    lifecycle: dict[str, Any]
    attendanceNote: str
    agenda: list[str]
    updates: list[dict[str, Any]]
    updateRequests: list[dict[str, Any]]
    viewerCanRequestUpdate: bool
    viewerCanVoteOnUpdateRequests: bool
    editRequests: list[dict[str, Any]]
    viewerCanRequestEdit: bool
    viewerCanVoteOnEditRequests: bool
    history: list[dict[str, Any]]
    attendees: list[str]
    invitedUsernames: list[str]
    eventEditors: list[dict[str, Any]]
    members: list[dict[str, Any]]
    viewerIsGoing: bool
    viewerCanToggleGoing: bool
    viewerHasEventEditAccess: bool
    viewerCanManageEditors: bool
    viewerCanShare: bool
    availableEditorInvitees: list[dict[str, Any]]
    shareContacts: list[dict[str, Any]]
    report: dict[str, Any] | None = None
    isRemovedByReport: bool
    discussionNote: str
    discussion: list[dict[str, Any]]


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


class EventEditorManageRequest(BaseModel):
    user_id: UUID


class EventValueCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=200)


class EventValueVoteRequest(BaseModel):
    importance: int = Field(ge=1, le=10)


class EventActivityRoleRequirementIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=100)
    required_count: int = Field(ge=1)
    maximum_count: int | None = Field(default=None, ge=1)


class EventActivityCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime
    ends_at: datetime
    location_label: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1)
    role_requirements: list[EventActivityRoleRequirementIn] = Field(default_factory=list)
    linked_plan_id: UUID | None = None
    linked_plan_phase_id: str | None = None


class EventActivityCommitRequest(BaseModel):
    role_label: str | None = Field(default=None, min_length=1, max_length=100)
    role_id: UUID | None = None


class ShareTargetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=32)


async def _get_optional_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UUID | None:
    if credentials is None or not credentials.credentials:
        return None

    try:
        payload = await get_current_user_token_payload(credentials)
    except HTTPException:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None

    try:
        return UUID(subject)
    except ValueError:
        return None


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


@router.get("/{slug}", response_model=EventDetailResponse)
async def get_event(
    slug: str,
    viewer_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await get_event_detail(db=db, cache=cache, slug=slug, current_user_id=viewer_user_id)


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


@router.post("/{slug}/editors/grant")
async def grant_editor_route(
    slug: str,
    payload: EventEditorManageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return grant_event_editor(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        target_user_id=payload.user_id,
    )


@router.post("/{slug}/editors/revoke")
async def revoke_editor_route(
    slug: str,
    payload: EventEditorManageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return revoke_event_editor(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        target_user_id=payload.user_id,
    )


@router.post("/{slug}/values")
async def create_event_value_route(
    slug: str,
    payload: EventValueCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_event_value(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        label=payload.label,
    )


@router.post("/{slug}/values/{value_id}/importance")
async def vote_event_value_route(
    slug: str,
    value_id: UUID,
    payload: EventValueVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_event_value_importance(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        value_id=value_id,
        importance=payload.importance,
    )


@router.post("/{slug}/activities")
async def create_event_activity_route(
    slug: str,
    payload: EventActivityCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_event_activity(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        title=payload.title,
        scheduled_at=payload.scheduled_at,
        ends_at=payload.ends_at,
        location_label=payload.location_label,
        note=payload.note,
        role_requirements=[item.model_dump() for item in payload.role_requirements],
        linked_plan_id=payload.linked_plan_id,
        linked_plan_phase_id=payload.linked_plan_phase_id,
    )


@router.post("/{slug}/activities/{activity_id}/commit")
async def commit_event_activity_route(
    slug: str,
    activity_id: UUID,
    payload: EventActivityCommitRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    role_label = payload.role_label
    if role_label is None and payload.role_id is not None:
        role_label = db.execute(
            select(event_activity_roles.c.label).where(
                event_activity_roles.c.id == payload.role_id,
                event_activity_roles.c.activity_id == activity_id,
            )
        ).scalar_one_or_none()
        if role_label is None:
            raise HTTPException(status_code=404, detail="Role not found")

    if role_label is None:
        raise HTTPException(status_code=422, detail="role_label is required")

    return commit_event_activity_role(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        activity_id=activity_id,
        role_label=role_label,
    )


@router.delete("/{slug}/activities/{activity_id}/commit")
async def uncommit_event_activity_route(
    slug: str,
    activity_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return uncommit_event_activity_role(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        activity_id=activity_id,
    )


@router.post("/{slug}/share")
async def share_event_route(
    slug: str,
    payload: ShareTargetRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return share_event_with_user(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        username=payload.username,
    )