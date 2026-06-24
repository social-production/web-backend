from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_db
from app.services.content import (
    create_post,
    create_thread,
    get_post_by_id,
    get_thread_by_slug,
)

router = APIRouter(prefix="/content", tags=["content"])


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    channel_slugs: list[str] = Field(default_factory=list, description="Channel slugs to tag this thread with")
    community_slugs: list[str] = Field(default_factory=list, description="Community slugs to tag this thread with")


class PostCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)
    audience: str = Field(default="public", pattern="^(public|followers)$")


class DiscussionCommentOut(BaseModel):
    id: UUID
    author_username: str = ""
    body: str
    vote_count: int
    active_vote: int = 0
    created_at: object
    replies: list["DiscussionCommentOut"] = Field(default_factory=list)


DiscussionCommentOut.model_rebuild()


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


class ChannelTagOut(BaseModel):
    slug: str
    label: str
    kind: str


class ThreadOut(BaseModel):
    id: UUID
    slug: str
    title: str
    body: str
    author_id: UUID | None = None
    author_username: str = ""
    vote_count: int
    active_vote: int = 0
    comment_count: int
    last_activity_at: object
    created_at: object
    updated_at: object
    channel_tags: list[ChannelTagOut] = Field(default_factory=list)
    community_tags: list[ChannelTagOut] = Field(default_factory=list)
    discussion: list[DiscussionCommentOut] = Field(default_factory=list)


class PostOut(BaseModel):
    id: UUID
    author_id: UUID | None = None
    author_username: str = ""
    author_profile_image_url: str | None = None
    body: str
    audience: str
    vote_count: int
    active_vote: int = 0
    comment_count: int
    created_at: object
    updated_at: object
    discussion: list[DiscussionCommentOut] = Field(default_factory=list)


class ThreadResponse(BaseModel):
    thread: ThreadOut


class PostResponse(BaseModel):
    post: PostOut


@router.post("/threads", response_model=ThreadResponse)
def create_new_thread(
    payload: ThreadCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_thread(db, current_user_id, payload.slug, payload.title, payload.body, payload.channel_slugs, payload.community_slugs)


@router.get("/threads/{slug}", response_model=ThreadResponse)
def get_thread(
    slug: str,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(_get_optional_user_id),
) -> dict[str, object]:
    return get_thread_by_slug(db, slug, current_user_id=current_user_id)


@router.post("/posts", response_model=PostResponse)
def create_new_post(
    payload: PostCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_post(db, current_user_id, payload.body, payload.audience)


@router.get("/posts/{post_id}", response_model=PostResponse)
def get_post(
    post_id: UUID,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(_get_optional_user_id),
) -> dict[str, object]:
    return get_post_by_id(db, post_id, current_user_id=current_user_id)
