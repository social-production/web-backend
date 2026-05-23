from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.users import (
    follow_user,
    get_followers,
    get_following,
    get_own_profile,
    get_profile_by_username,
    unfollow_user,
    update_own_profile_settings,
)

router = APIRouter(prefix="/users", tags=["users"])


class UserSummary(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profile_image_url: str | None = None
    is_active: bool


class UserSettings(BaseModel):
    appearance_theme_mode: str
    default_feed: str
    public_feed_scope: str
    public_feed_filter: str
    public_feed_sort: str
    public_feed_window: str
    personal_feed_scope: str
    personal_feed_filter: str
    personal_feed_sort: str
    personal_feed_window: str
    hide_public_activity_from_personal_feeds: bool
    hide_personal_feed_from_non_followers: bool
    require_follow_approval: bool


class PublicProfileResponse(BaseModel):
    user: UserSummary


class OwnProfileResponse(BaseModel):
    user: UserSummary
    settings: UserSettings


class UpdateOwnProfileSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bio: str | None = Field(default=None, max_length=500)
    profile_image_url: str | None = Field(default=None, max_length=2000)
    appearance_theme_mode: str | None = None
    default_feed: str | None = None
    public_feed_scope: str | None = None
    public_feed_filter: str | None = None
    public_feed_sort: str | None = None
    public_feed_window: str | None = None
    personal_feed_scope: str | None = None
    personal_feed_filter: str | None = None
    personal_feed_sort: str | None = None
    personal_feed_window: str | None = None
    hide_public_activity_from_personal_feeds: bool | None = None
    hide_personal_feed_from_non_followers: bool | None = None
    require_follow_approval: bool | None = None


class FollowResponse(BaseModel):
    ok: bool
    following: bool
    username: str


class FollowUserSummary(UserSummary):
    follow_status: str


class FollowListResponse(BaseModel):
    username: str
    total: int
    items: list[FollowUserSummary]


@router.get("/me", response_model=OwnProfileResponse)
def get_me(current_user_id: UUID = Depends(get_current_user_id), db: Session = Depends(get_db)) -> dict[str, object]:
    return get_own_profile(db, current_user_id)


@router.patch("/me/settings", response_model=OwnProfileResponse)
def patch_my_settings(
    payload: UpdateOwnProfileSettingsRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    return update_own_profile_settings(db, current_user_id, updates)


@router.post("/{username}/follow", response_model=FollowResponse)
def follow(username: str, current_user_id: UUID = Depends(get_current_user_id), db: Session = Depends(get_db)) -> dict[str, object]:
    return follow_user(db, current_user_id, username)


@router.delete("/{username}/follow", response_model=FollowResponse)
def unfollow(username: str, current_user_id: UUID = Depends(get_current_user_id), db: Session = Depends(get_db)) -> dict[str, object]:
    return unfollow_user(db, current_user_id, username)


@router.get("/{username}", response_model=PublicProfileResponse)
def get_profile(username: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return get_profile_by_username(db, username)


@router.get("/{username}/followers", response_model=FollowListResponse)
def followers(username: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return get_followers(db, username)


@router.get("/{username}/following", response_model=FollowListResponse)
def following(username: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return get_following(db, username)
