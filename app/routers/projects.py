from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_cache, get_db
from app.services.projects import (
    create_project,
    get_project_by_slug,
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


@router.get("/{slug}", response_model=ProjectResponse)
async def get_project(
    slug: str,
    db: Session = Depends(get_db),
    cache: Redis = Depends(get_cache),
) -> dict[str, object]:
    return await get_project_by_slug(db=db, cache=cache, slug=slug)


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
