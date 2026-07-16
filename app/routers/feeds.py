from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.feeds import (
    get_home_feed,
    get_personal_feed,
    get_public_feed,
    get_scope_feed,
    get_user_feed,
)

router = APIRouter(prefix="/feeds", tags=["feeds"])


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


class FeedResponse(BaseModel):
    total: int
    sort: str
    limit: int
    offset: int
    items: list[FeedItemOut]


@router.get("/public", response_model=FeedResponse)
def public_feed(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_public_feed(
        db=db, sort=sort, limit=limit, offset=offset, current_user_id=current_user_id
    )


@router.get("/home", response_model=FeedResponse)
def home_feed(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
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
    )


@router.get("/personal", response_model=FeedResponse)
def personal_feed(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
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
    )


@router.get("/scope", response_model=FeedResponse)
def scope_feed(
    kind: str = Query(pattern="^(channel|community)$"),
    slug: str = Query(min_length=1),
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
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
    )


@router.get("/user/{username}", response_model=FeedResponse)
def user_feed(
    username: str,
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
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
    )
