from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.content import (
    commit_help_request_role,
    create_help_request,
    create_post,
    create_thread,
    get_help_request_by_id,
    get_post_by_id,
    get_thread_by_slug,
    uncommit_help_request_role,
)

router = APIRouter(prefix="/content", tags=["content"])


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    channel_slugs: list[str] = Field(
        default_factory=list, description="Channel slugs to tag this thread with"
    )
    community_slugs: list[str] = Field(
        default_factory=list, description="Community slugs to tag this thread with"
    )


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
    replies: list[DiscussionCommentOut] = Field(default_factory=list)


class TagRefOut(BaseModel):
    slug: str
    label: str
    kind: str


DiscussionCommentOut.model_rebuild()


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


class HelpRequestRole(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    slots: int = Field(ge=0)


class HelpRequestCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    location_label: str = Field(min_length=1, max_length=200)
    needed_at: datetime
    roles: list[HelpRequestRole] = Field(min_length=1)
    channel_slugs: list[str] = Field(default_factory=list)
    community_slugs: list[str] = Field(default_factory=list)


class HelpRequestRoleOut(BaseModel):
    role_id: UUID
    title: str
    description: str = ""
    slots: int
    filled_count: int = 0
    is_viewer_assigned: bool = False


class HelpRequestOut(BaseModel):
    id: UUID
    author_id: UUID | None = None
    author_username: str = ""
    title: str
    body: str
    location_label: str
    schedule_label: str
    needed_at: object
    roles: list[HelpRequestRoleOut]
    vote_count: int = 0
    comment_count: int = 0
    active_vote: int = 0
    discussion: list[dict[str, object]] = Field(default_factory=list)
    channel_tags: list[TagRefOut] = Field(default_factory=list)
    community_tags: list[TagRefOut] = Field(default_factory=list)
    created_at: object


class HelpRequestActionResponse(BaseModel):
    ok: bool = True
    help_request_id: UUID
    role_id: UUID


class HelpRequestResponse(BaseModel):
    help_request: HelpRequestOut


@router.post("/threads", response_model=ThreadResponse)
def create_new_thread(
    payload: ThreadCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_thread(
        db,
        current_user_id,
        payload.slug,
        payload.title,
        payload.body,
        payload.channel_slugs,
        payload.community_slugs,
    )


@router.get("/threads/{slug}", response_model=ThreadResponse)
def get_thread(
    slug: str,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
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
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_post_by_id(db, post_id, current_user_id=current_user_id)


@router.post("/help-requests", response_model=HelpRequestResponse)
def create_new_help_request(
    payload: HelpRequestCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_help_request(
        db=db,
        current_user_id=current_user_id,
        title=payload.title,
        body=payload.body,
        location_label=payload.location_label,
        needed_at=payload.needed_at,
        roles=[role.model_dump() for role in payload.roles],
        channel_slugs=payload.channel_slugs,
        community_slugs=payload.community_slugs,
    )


@router.get("/help-requests/{help_request_id}", response_model=HelpRequestResponse)
def get_help_request(
    help_request_id: UUID,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_help_request_by_id(db, help_request_id, current_user_id=current_user_id)


@router.post(
    "/help-requests/{help_request_id}/roles/{role_id}/commit",
    response_model=HelpRequestActionResponse,
)
def commit_help_request_role_endpoint(
    help_request_id: UUID,
    role_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return commit_help_request_role(db, current_user_id, help_request_id, role_id)


@router.delete(
    "/help-requests/{help_request_id}/roles/{role_id}/commit",
    response_model=HelpRequestActionResponse,
)
def uncommit_help_request_role_endpoint(
    help_request_id: UUID,
    role_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return uncommit_help_request_role(db, current_user_id, help_request_id, role_id)
