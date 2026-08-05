from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.detail_links import (
    create_detail_link_request,
    create_detail_link_sever_request,
    vote_detail_link_request,
)

router = APIRouter(prefix="/events", tags=["events-links"])


class LinkRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_kind: str = Field(pattern="^(project|event)$")
    target_slug: str = Field(min_length=1, max_length=120)
    relationship_label: str | None = Field(default=None, max_length=120)
    summary: str = Field(min_length=1)


class LinkRequestVoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class LinkSeverCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary: str | None = Field(default=None, max_length=2000)


@router.post("/{slug}/manual-links")
def create_event_link_request_route(
    slug: str,
    payload: LinkRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_detail_link_request(
        db=db,
        current_user_id=current_user_id,
        source_kind="event",
        source_slug=slug,
        target_kind=payload.target_kind,
        target_slug=payload.target_slug,
        relationship_label=payload.relationship_label,
        summary=payload.summary,
    )


@router.post("/{slug}/manual-links/{request_id}/vote")
def vote_event_link_request_route(
    slug: str,
    request_id: UUID,
    payload: LinkRequestVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_detail_link_request(
        db=db,
        current_user_id=current_user_id,
        subject_kind="event",
        subject_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/links/{link_id}/sever")
def create_event_link_sever_request_route(
    slug: str,
    link_id: UUID,
    payload: LinkSeverCreateIn | None = None,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_detail_link_sever_request(
        db=db,
        current_user_id=current_user_id,
        subject_kind="event",
        subject_slug=slug,
        link_id=link_id,
        summary=(payload.summary if payload else None),
    )
