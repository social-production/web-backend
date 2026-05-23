from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.scopes import (
    create_channel,
    create_community,
    get_channel_by_slug,
    get_community_by_slug,
    join_scope,
    leave_scope,
    list_scope_members,
)

router = APIRouter(prefix="/scopes", tags=["scopes"])


class ChannelCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)


class CommunityCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    join_policy: str = Field(default="open", min_length=1, max_length=16)


class ScopeJoinResponse(BaseModel):
    ok: bool
    joined: bool
    scope_kind: str
    slug: str


class ChannelResponse(BaseModel):
    channel: dict[str, object]
    member_count: int | None = None


class CommunityResponse(BaseModel):
    community: dict[str, object]
    member_count: int | None = None


class ScopeMember(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profile_image_url: str | None = None
    is_active: bool
    role: str
    joined_at: object


class ScopeMembersResponse(BaseModel):
    scope_kind: str
    slug: str
    total: int
    items: list[ScopeMember]


@router.post("/channels", dependencies=[Depends(get_current_user_id)], response_model=ChannelResponse)
def create_new_channel(
    payload: ChannelCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_channel(db, current_user_id, payload.slug, payload.name, payload.description)


@router.post("/communities", dependencies=[Depends(get_current_user_id)], response_model=CommunityResponse)
def create_new_community(
    payload: CommunityCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_community(db, current_user_id, payload.slug, payload.name, payload.description, payload.join_policy)


@router.get("/channels/{slug}", response_model=ChannelResponse)
def get_channel(slug: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return get_channel_by_slug(db, slug)


@router.get("/communities/{slug}", response_model=CommunityResponse)
def get_community(slug: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return get_community_by_slug(db, slug)


@router.post("/channels/{slug}/join", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse)
def join_channel(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_scope(db, current_user_id, "channel", slug)


@router.delete("/channels/{slug}/leave", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse)
def leave_channel(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return leave_scope(db, current_user_id, "channel", slug)


@router.post("/communities/{slug}/join", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse)
def join_community(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_scope(db, current_user_id, "community", slug)


@router.delete("/communities/{slug}/leave", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse)
def leave_community(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return leave_scope(db, current_user_id, "community", slug)


@router.get("/channels/{slug}/members", dependencies=[Depends(get_current_user_id)], response_model=ScopeMembersResponse)
def channel_members(slug: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return list_scope_members(db, "channel", slug)


@router.get("/communities/{slug}/members", dependencies=[Depends(get_current_user_id)], response_model=ScopeMembersResponse)
def community_members(slug: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return list_scope_members(db, "community", slug)
