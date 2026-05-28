from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_links import (
    create_project_link_request,
    vote_project_link_request,
)

router = APIRouter(prefix="/projects", tags=["projects-links"])


class LinkRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_project_slug: str = Field(min_length=1, max_length=120)
    relationship_label: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1)


class LinkRequestVoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


@router.post("/{slug}/manual-links")
def create_project_link_request_route(
    slug: str,
    payload: LinkRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_project_link_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        target_project_slug=payload.target_project_slug,
        relationship_label=payload.relationship_label,
        summary=payload.summary,
    )


@router.post("/{slug}/manual-links/{request_id}/vote")
def vote_project_link_request_route(
    slug: str,
    request_id: UUID,
    payload: LinkRequestVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_project_link_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )
