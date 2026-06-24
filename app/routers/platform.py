from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_token_payload
from app.dependencies import get_db
from app.services.platform import get_platform_page

router = APIRouter(tags=["platform"])


class PlatformChannelOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str


class PlatformBoardPersonOut(BaseModel):
    user_id: UUID
    username: str
    standing_state: str
    updated_at: object
    yes_count: int
    no_count: int
    vote_count: int
    approval_ratio: float
    required_quorum: int = 0
    weekly_active_users: int = 0
    active_vote: str | None = None


class PlatformCandidacyOptionsOut(BaseModel):
    viewer_state: str | None = None
    can_volunteer: bool


class PlatformFeedItemOut(BaseModel):
    id: UUID
    entity_type: str
    slug: str
    title: str
    body: str
    author_id: UUID | None = None
    signal_count: int
    vote_count: int
    comment_count: int
    member_count: int
    going_count: int
    last_activity_at: object
    created_at: object


class PlatformFeedOut(BaseModel):
    total: int
    sort: str
    limit: int
    offset: int
    items: list[PlatformFeedItemOut]


class PlatformPageResponse(BaseModel):
    channel: PlatformChannelOut | None = None
    moderators: list[PlatformBoardPersonOut]
    moderator_candidates: list[PlatformBoardPersonOut]
    moderator_candidacy_options: PlatformCandidacyOptionsOut | None = None
    feed: PlatformFeedOut


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


@router.get("/platform", response_model=PlatformPageResponse)
def platform_page(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    viewer_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_platform_page(
        db=db,
        viewer_user_id=viewer_user_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )
