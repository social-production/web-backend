from __future__ import annotations

from datetime import datetime
from uuid import UUID
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_cache, get_db
from app.services.projects import (
    add_project_value,
    add_project_update,
    commit_project_activity_role,
    create_project,
    create_project_activity,
    get_project_detail,
    join_project,
    leave_project,
    share_project_with_user,
    toggle_project_signal,
    uncommit_project_activity_role,
    vote_project_value_importance,
)
from app.models import project_activity_roles

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


class ProjectValueCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=200)


class ProjectValueVoteRequest(BaseModel):
    importance: int = Field(ge=1, le=10)


class ProjectActivityRoleRequirementIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=100)
    required_count: int = Field(ge=1)
    maximum_count: int | None = Field(default=None, ge=1)


class ProjectActivityCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime
    ends_at: datetime
    location_label: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1)
    role_requirements: list[ProjectActivityRoleRequirementIn] = Field(default_factory=list)
    linked_plan_id: UUID | None = None
    linked_plan_phase_id: str | None = None


class ProjectActivityCommitRequest(BaseModel):
    role_label: str | None = Field(default=None, min_length=1, max_length=100)
    role_id: UUID | None = None


class ProjectUpdateCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


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


@router.post("/{slug}/values")
async def create_value_route(
    slug: str,
    payload: ProjectValueCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_project_value(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        label=payload.label,
    )


@router.post("/{slug}/values/{value_id}/importance")
async def vote_value_importance_route(
    slug: str,
    value_id: UUID,
    payload: ProjectValueVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_project_value_importance(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        value_id=value_id,
        importance=payload.importance,
    )


@router.post("/{slug}/activities")
async def create_activity_route(
    slug: str,
    payload: ProjectActivityCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_project_activity(
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
async def commit_activity_role_route(
    slug: str,
    activity_id: UUID,
    payload: ProjectActivityCommitRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    role_label = payload.role_label
    if role_label is None and payload.role_id is not None:
        role_label = db.execute(
            select(project_activity_roles.c.label).where(
                project_activity_roles.c.id == payload.role_id,
                project_activity_roles.c.activity_id == activity_id,
            )
        ).scalar_one_or_none()
        if role_label is None:
            raise HTTPException(status_code=404, detail="Role not found")

    if role_label is None:
        raise HTTPException(status_code=422, detail="role_label is required")

    return commit_project_activity_role(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        activity_id=activity_id,
        role_label=role_label,
    )


@router.delete("/{slug}/activities/{activity_id}/commit")
async def uncommit_activity_role_route(
    slug: str,
    activity_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return uncommit_project_activity_role(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        activity_id=activity_id,
    )


@router.post("/{slug}/updates")
async def add_project_update_route(
    slug: str,
    payload: ProjectUpdateCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_project_update(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        title=payload.title,
        body=payload.body,
    )


@router.post("/{slug}/share")
async def share_project_route(
    slug: str,
    payload: ShareTargetRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return share_project_with_user(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        username=payload.username,
    )
