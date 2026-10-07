from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db, get_people_suggestions_provider
from app.ports import PeopleSuggestionsProvider
from app.services.users import (
    accept_follow_request,
    follow_user,
    get_follow_requests,
    get_followers,
    get_following,
    get_own_profile,
    get_profile_by_username,
    reject_follow_request,
    set_account_stance,
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


class TrustPersonOut(BaseModel):
    username: str
    profile_image_url: str | None = None


class AccountTrustOut(BaseModel):
    real_r: float
    vouch_weight: float
    bot_weight: float
    bootstrap_floor: bool
    bootstrap_floor_value: float | None = None
    vouchers: list[TrustPersonOut]
    bot_markers: list[TrustPersonOut]
    viewer_stance: str | None = None
    viewer_can_vouch: bool = False
    viewer_can_mark_bot: bool = False
    viewer_can_clear: bool = False


class StanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stance: Literal["vouch", "bot", "clear"]


class PeopleSuggestionOut(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profileImageUrl: str | None = None
    realR: float | None = None
    bootstrapFloor: bool = False


class PeopleSuggestionsResponse(BaseModel):
    items: list[PeopleSuggestionOut]


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
    hide_public_profile_activity_from_non_followers: bool
    require_follow_approval: bool
    preferred_language: str
    display_timezone: str | None = None
    combine_feeds: bool = False
    text_size: str = "medium"
    default_location_id: UUID | None = None
    notification_categories: list[str]


class PublicProfileResponse(BaseModel):
    user: UserSummary
    viewer_is_following: bool
    viewer_follow_status: str | None = None
    is_own_profile: bool
    can_view_personal_feed: bool
    can_view_public_profile_activity: bool
    trust: AccountTrustOut


class OwnProfileResponse(BaseModel):
    user: UserSummary
    settings: UserSettings


class UpdateOwnProfileSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bio: str | None = Field(default=None, max_length=500)
    profile_image_url: str | None = Field(default=None, max_length=500_000)
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
    hide_public_profile_activity_from_non_followers: bool | None = None
    require_follow_approval: bool | None = None
    preferred_language: str | None = None
    display_timezone: str | None = None
    combine_feeds: bool | None = None
    text_size: str | None = None
    default_location_id: UUID | None = None
    notification_categories: list[str] | None = None


class FollowResponse(BaseModel):
    ok: bool
    following: bool
    follow_status: str | None = None
    username: str


class FollowUserSummary(UserSummary):
    follow_status: str
    real_r: float | None = None
    bootstrap_floor: bool = False


class FollowListResponse(BaseModel):
    username: str
    total: int
    items: list[FollowUserSummary]


class FollowRequestListResponse(BaseModel):
    total: int
    items: list[FollowUserSummary]


class FollowRequestActionResponse(BaseModel):
    ok: bool
    username: str
    follow_status: str | None = None


@router.get("/me", response_model=OwnProfileResponse)
def get_me(
    current_user_id: UUID = Depends(get_current_user_id), db: Session = Depends(get_db)
) -> dict[str, object]:
    return get_own_profile(db, current_user_id)


@router.patch("/me/settings", response_model=OwnProfileResponse)
def patch_my_settings(
    payload: UpdateOwnProfileSettingsRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    return update_own_profile_settings(db, current_user_id, updates)


@router.get("/me/follow-requests", response_model=FollowRequestListResponse)
def follow_requests(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_follow_requests(db, current_user_id)


@router.post("/me/follow-requests/{username}/accept", response_model=FollowRequestActionResponse)
def accept_follow(
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return accept_follow_request(db, current_user_id, username)


@router.post("/me/follow-requests/{username}/reject", response_model=FollowRequestActionResponse)
def reject_follow(
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return reject_follow_request(db, current_user_id, username)


@router.post("/{username}/follow", response_model=FollowResponse)
def follow(
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return follow_user(db, current_user_id, username)


@router.delete("/{username}/follow", response_model=FollowResponse)
def unfollow(
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return unfollow_user(db, current_user_id, username)


@router.get("/suggestions", response_model=PeopleSuggestionsResponse)
def people_suggestions(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=12, ge=1, le=40),
    current_user_id: UUID = Depends(get_current_user_id),
    people: PeopleSuggestionsProvider = Depends(get_people_suggestions_provider),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    items = people.suggest(
        current_user_id,
        query=q,
        limit=limit,
    )
    from app.services.trust import trust_summaries

    summaries = trust_summaries(db)
    for item in items:
        summary = summaries.get(item["id"])
        if summary is None:
            continue
        item["realR"] = summary["real_r"]
        item["bootstrapFloor"] = summary["bootstrap_floor"]
    return {"items": items}


@router.post("/{username}/stance", response_model=AccountTrustOut)
def post_account_stance(
    username: str,
    payload: StanceRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return set_account_stance(db, current_user_id, username, payload.stance)


@router.get("/{username}", response_model=PublicProfileResponse)
def get_profile(
    username: str,
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_profile_by_username(db, username, current_user_id)


@router.get("/{username}/followers", response_model=FollowListResponse)
def followers(
    username: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_followers(db, username, current_user_id, limit=limit, offset=offset)


@router.get("/{username}/following", response_model=FollowListResponse)
def following(
    username: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_following(db, username, current_user_id, limit=limit, offset=offset)
