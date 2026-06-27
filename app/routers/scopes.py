from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_db
from app.services.scopes import (
    create_channel,
    create_community,
    create_scope_invite,
    get_channel_by_slug,
    get_community_by_slug,
    join_scope,
    leave_scope,
    list_taggable_scopes,
    list_scope_members,
    redeem_scope_invite,
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


class InviteRedeemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=1)


class ChannelResponse(BaseModel):
    channel: dict[str, object]
    member_count: int | None = None
    viewer_is_member: bool = False


class CommunityResponse(BaseModel):
    community: dict[str, object]
    member_count: int | None = None
    viewer_is_member: bool = False
    invite_link: str | None = None


class ScopeInviteResponse(BaseModel):
    token: str
    redeem_url: str


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


class TaggableScopeItem(BaseModel):
    slug: str
    label: str
    href: str
    visibility: str
    viewer_is_member: bool


class TaggableScopesResponse(BaseModel):
    channels: list[TaggableScopeItem]
    communities: list[TaggableScopeItem]


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


@router.get("/taggable", dependencies=[Depends(get_current_user_id)], response_model=TaggableScopesResponse)
def get_taggable_scopes(
    q: str = "",
    kind: str | None = None,
    limit: int = 8,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_taggable_scopes(db=db, current_user_id=current_user_id, query=q, kind=kind, limit=limit)


@router.get("/channels/{slug}", response_model=ChannelResponse)
def get_channel(
    slug: str,
    current_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_channel_by_slug(db, slug, current_user_id)


@router.get("/communities/{slug}", response_model=CommunityResponse)
def get_community(
    slug: str,
    current_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_community_by_slug(db, slug, current_user_id)


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


@router.post("/invites/redeem", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse)
def redeem_invite(
    payload: InviteRedeemRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return redeem_scope_invite(db=db, current_user_id=current_user_id, token=payload.token)


@router.post(
    "/{scope_kind}/{slug}/invites",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeInviteResponse,
)
def create_scope_invite_route(
    scope_kind: str,
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    normalized_kind = scope_kind.strip().lower()
    if normalized_kind not in {"channel", "community"}:
        raise HTTPException(status_code=422, detail="scope_kind must be channel or community")
    return create_scope_invite(db, current_user_id, normalized_kind, slug)
