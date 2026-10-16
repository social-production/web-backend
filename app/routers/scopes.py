from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.scopes import (
    create_channel,
    create_community,
    create_scope_invite,
    get_channel_by_slug,
    get_community_by_slug,
    invite_user_to_community,
    join_scope,
    leave_scope,
    list_discoverable_scopes,
    list_scope_members,
    list_taggable_scopes,
    redeem_scope_invite,
)

router = APIRouter(prefix="/scopes", tags=["scopes"])


class ChannelCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=3, max_length=32)
    description: str = Field(min_length=1, max_length=2000)


class CommunityCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    slug: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=3, max_length=32)
    description: str = Field(min_length=1, max_length=2000)
    join_policy: str = Field(default="open", min_length=1, max_length=16)
    governance: str = Field(default="collaborative", pattern="^(collaborative|organizer)$")


class ScopeJoinResponse(BaseModel):
    ok: bool
    joined: bool
    scope_kind: str
    slug: str


class InviteRedeemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=1)


class CommunityInviteUserRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=32)


class CommunityInviteUserResponse(BaseModel):
    ok: bool
    username: str
    already_member: bool = False


class ChannelResponse(BaseModel):
    channel: dict[str, object]
    member_count: int | None = None
    viewer_is_member: bool = False
    open_votes: int = 0


class CommunityResponse(BaseModel):
    community: dict[str, object]
    member_count: int | None = None
    viewer_is_member: bool = False
    open_votes: int = 0
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


class DiscoverScopeItem(BaseModel):
    slug: str
    label: str
    href: str
    description: str
    visibility: str
    member_count: int
    viewer_is_member: bool


class DiscoverScopesResponse(BaseModel):
    kind: str
    items: list[DiscoverScopeItem]


@router.post(
    "/channels", dependencies=[Depends(get_current_user_id)], response_model=ChannelResponse
)
def create_new_channel(
    payload: ChannelCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_channel(db, current_user_id, payload.slug, payload.name, payload.description)


@router.post(
    "/communities", dependencies=[Depends(get_current_user_id)], response_model=CommunityResponse
)
def create_new_community(
    payload: CommunityCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_community(
        db,
        current_user_id,
        payload.slug,
        payload.name,
        payload.description,
        payload.join_policy,
        payload.governance,
    )


@router.get(
    "/taggable", dependencies=[Depends(get_current_user_id)], response_model=TaggableScopesResponse
)
def get_taggable_scopes(
    q: str = "",
    kind: str | None = None,
    limit: int = 8,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_taggable_scopes(
        db=db, current_user_id=current_user_id, query=q, kind=kind, limit=limit
    )


@router.get("/discover", response_model=DiscoverScopesResponse)
def get_discoverable_scopes(
    kind: str,
    limit: int = 200,
    q: str = "",
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_discoverable_scopes(
        db=db, current_user_id=current_user_id, kind=kind, limit=limit, query=q
    )


@router.get("/channels/{slug}", response_model=ChannelResponse)
def get_channel(
    slug: str,
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_channel_by_slug(db, slug, current_user_id)


@router.get("/communities/{slug}", response_model=CommunityResponse)
def get_community(
    slug: str,
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_community_by_slug(db, slug, current_user_id)


@router.post(
    "/channels/{slug}/join",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeJoinResponse,
)
def join_channel(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_scope(db, current_user_id, "channel", slug)


@router.delete(
    "/channels/{slug}/leave",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeJoinResponse,
)
def leave_channel(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return leave_scope(db, current_user_id, "channel", slug)


@router.post(
    "/communities/{slug}/join",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeJoinResponse,
)
def join_community(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return join_scope(db, current_user_id, "community", slug)


@router.delete(
    "/communities/{slug}/leave",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeJoinResponse,
)
def leave_community(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return leave_scope(db, current_user_id, "community", slug)


@router.get(
    "/channels/{slug}/members",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeMembersResponse,
)
def channel_members(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_scope_members(db, "channel", slug, current_user_id)


@router.get(
    "/communities/{slug}/members",
    dependencies=[Depends(get_current_user_id)],
    response_model=ScopeMembersResponse,
)
def community_members(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_scope_members(db, "community", slug, current_user_id)


@router.post(
    "/communities/{slug}/invite-user",
    dependencies=[Depends(get_current_user_id)],
    response_model=CommunityInviteUserResponse,
)
def invite_user_to_community_route(
    slug: str,
    payload: CommunityInviteUserRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return invite_user_to_community(
        db=db,
        current_user_id=current_user_id,
        slug=slug,
        username=payload.username,
    )


@router.post(
    "/invites/redeem", dependencies=[Depends(get_current_user_id)], response_model=ScopeJoinResponse
)
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


# ---- settings, pictures and member votes (app/services/scope_governance.py) -------------


class ScopeEditRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=3, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    image_key: str | None = Field(default=None, max_length=80)
    image_content_type: str | None = Field(default=None, max_length=127)
    remove_image: bool | None = None


class ScopeProposalRequest(BaseModel):
    kind: str = Field(pattern="^(remove_member|to_collaborative|to_organizer)$")
    target_username: str | None = None
    organizer_usernames: list[str] = Field(default_factory=list)


class ScopeVoteRequest(BaseModel):
    vote: str = Field(pattern="^(yes|no)$")


class ScopeOrganizerRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    organizer: bool


def _scope_kind(plural: str) -> str:
    if plural == "channels":
        return "channel"
    if plural == "communities":
        return "community"
    raise HTTPException(status_code=404, detail="Not found")


@router.get("/{plural}/{slug}/settings")
def get_scope_settings(
    plural: str,
    slug: str,
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import settings_payload

    return settings_payload(db, current_user_id, _scope_kind(plural), slug)


@router.patch("/{plural}/{slug}/settings")
def patch_scope_settings(
    plural: str,
    slug: str,
    payload: ScopeEditRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import edit

    return edit(db, current_user_id, _scope_kind(plural), slug, payload.model_dump())


@router.post("/{plural}/{slug}/image")
async def upload_scope_image(
    plural: str,
    slug: str,
    file: UploadFile = File(...),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import MAX_IMAGE_BYTES, store_image

    data = await file.read(MAX_IMAGE_BYTES + 1)
    return store_image(db, current_user_id, _scope_kind(plural), slug, file.content_type, data)


@router.get("/{plural}/{slug}/image")
def get_scope_image(
    plural: str,
    slug: str,
    key: str | None = None,
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    from app.services.scope_governance import read_image

    data, content_type = read_image(db, current_user_id, _scope_kind(plural), slug, key)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/{plural}/{slug}/proposals")
def post_scope_proposal(
    plural: str,
    slug: str,
    payload: ScopeProposalRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from sqlalchemy import select

    from app.models import users
    from app.services.scope_governance import propose

    body: dict[str, object] = {}
    if payload.target_username:
        target = db.execute(
            select(users.c.id).where(users.c.username == payload.target_username)
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="Member not found")
        body = {"target_user_id": str(target), "username": payload.target_username}
    if payload.organizer_usernames:
        ids = db.execute(
            select(users.c.id).where(users.c.username.in_(payload.organizer_usernames))
        ).scalars()
        body["organizer_ids"] = [str(value) for value in ids]
        body["organizer_usernames"] = payload.organizer_usernames
    return propose(db, current_user_id, _scope_kind(plural), slug, payload.kind, body)


@router.post("/proposals/{proposal_id}/vote")
def post_scope_vote(
    proposal_id: UUID,
    payload: ScopeVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import vote

    return vote(db, current_user_id, proposal_id, payload.vote)


@router.post("/{plural}/{slug}/organizers")
def post_scope_organizer(
    plural: str,
    slug: str,
    payload: ScopeOrganizerRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import set_organizer

    return set_organizer(
        db, current_user_id, _scope_kind(plural), slug, payload.username, payload.organizer
    )


@router.delete("/{plural}/{slug}/members/{username}")
def delete_scope_member(
    plural: str,
    slug: str,
    username: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.services.scope_governance import remove_member

    return remove_member(db, current_user_id, _scope_kind(plural), slug, username)
