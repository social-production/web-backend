from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.invites import list_pending_invites, respond_to_invite

router = APIRouter(prefix="/invites", tags=["invites"])


class PendingInviteOut(BaseModel):
    id: str
    kind: str
    title: str
    description: str
    href: str
    invited_by: str | None = None
    created_at: str
    scheduled_at: str | None = None
    ends_at: str | None = None


class PendingInvitesResponse(BaseModel):
    items: list[PendingInviteOut]


class InviteAnswerResponse(BaseModel):
    ok: bool
    accepted: bool
    href: str | None = None


@router.get("", response_model=PendingInvitesResponse)
def get_pending_invites(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {"items": list_pending_invites(db, current_user_id)}


@router.post("/{invite_id}/accept", response_model=InviteAnswerResponse)
def accept_invite(
    invite_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return respond_to_invite(db, current_user_id, invite_id, accept=True)


@router.post("/{invite_id}/decline", response_model=InviteAnswerResponse)
def decline_invite(
    invite_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return respond_to_invite(db, current_user_id, invite_id, accept=False)
