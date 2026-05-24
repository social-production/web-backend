from __future__ import annotations

from uuid import UUID
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_cache, get_db
from app.services.projects import (
    create_project,
    get_project_detail,
    join_project,
    leave_project,
    toggle_project_signal,
)

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    project_mode: str = Field(pattern="^(productive|collective-service|personal-service)$")
    project_subtype: str | None = Field(default=None, pattern="^(standard|software)$")
    location_label: str = Field(min_length=1, max_length=160)
    channel_slugs: list[str] = Field(default_factory=list)


class ProjectTagOut(BaseModel):
    id: UUID
    tag_kind: str
    channel_id: UUID | None = None
    community_id: UUID | None = None


class SignalCountsOut(BaseModel):
    demand: int
    opposition: int
    total: int


class ProjectOut(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str
    author_id: UUID | None = None
    project_mode: str
    project_subtype: str | None = None
    current_phase_id: str
    stage_label: str
    location_label: str
    is_platform_tagged: bool
    is_closed: bool
    close_outcome: str | None = None
    signal_count: int
    vote_count: int
    comment_count: int
    member_count: int
    last_activity_at: object
    created_at: object
    updated_at: object
    tags: list[ProjectTagOut]
    signals: SignalCountsOut


class ProjectResponse(BaseModel):
    project: ProjectOut


class ProjectDetailResponse(BaseModel):
    id: str
    slug: str
    createdAt: str
    title: str
    authorUsername: str
    projectMode: str
    projectSubtype: str | None = None
    description: str
    channelTags: list[dict[str, Any]]
    communityTags: list[dict[str, Any]]
    stage: str
    locationLabel: str
    voteCount: int
    activeVote: int
    signalCount: int
    commentCount: int
    memberCount: int
    lastActivityAt: str
    lifecycle: dict[str, Any]
    updates: list[dict[str, Any]]
    updateRequests: list[dict[str, Any]]
    viewerCanRequestUpdate: bool
    viewerCanVoteOnUpdateRequests: bool
    editRequests: list[dict[str, Any]]
    viewerCanRequestEdit: bool
    viewerCanVoteOnEditRequests: bool
    linksFrame: dict[str, Any]
    inventoryFrame: dict[str, Any] | None = None
    history: list[dict[str, Any]]
    projectManagers: list[dict[str, Any]]
    members: list[dict[str, Any]]
    viewerIsMember: bool
    viewerCanToggleMembership: bool
    viewerCanShare: bool
    viewerCanToggleManagerNomination: bool
    viewerIsManagerCandidate: bool
    viewerIsProjectManager: bool
    shareContacts: list[dict[str, Any]]
    report: dict[str, Any] | None = None
    isRemovedByReport: bool
    discussionNote: str
    discussion: list[dict[str, Any]]


class ProjectMembershipResponse(BaseModel):
    ok: bool
    joined: bool
    slug: str


class ProjectSignalToggleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    signal_type: str = Field(pattern="^(demand|opposition)$")


class ProjectSignalToggleResponse(BaseModel):
    ok: bool
    slug: str
    action: str
    signal_type: str
    signals: SignalCountsOut


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


@router.post("", response_model=ProjectResponse)
async def create_new_project(
    payload: ProjectCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_project(
        db=db,
        current_user_id=current_user_id,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        project_mode=payload.project_mode,
        project_subtype=payload.project_subtype,
        location_label=payload.location_label,
        channel_slugs=payload.channel_slugs,
    )


@router.get("/{slug}", response_model=ProjectDetailResponse)
async def get_project(
    slug: str,
    viewer_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await get_project_detail(db=db, cache=cache, slug=slug, current_user_id=viewer_user_id)


@router.post("/{slug}/join", response_model=ProjectMembershipResponse)
async def join_project_route(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_project(db=db, current_user_id=current_user_id, slug=slug)


@router.delete("/{slug}/leave", response_model=ProjectMembershipResponse)
async def leave_project_route(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return leave_project(db=db, current_user_id=current_user_id, slug=slug)


@router.post("/{slug}/signals", response_model=ProjectSignalToggleResponse)
async def toggle_project_signal_route(
    slug: str,
    payload: ProjectSignalToggleRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await toggle_project_signal(
        db=db,
        cache=cache,
        current_user_id=current_user_id,
        slug=slug,
        signal_type=payload.signal_type,
    )
