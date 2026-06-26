from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_service_request_settings import (
    create_settings_change_request,
    vote_settings_change_request,
)

router = APIRouter(prefix="/projects", tags=["projects-service-request-settings"])


class SettingsChangeRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = ""
    enabled: bool
    request_mode: str = Field(pattern="^(calendar|direct|both)$")
    allow_off_schedule_requests: bool


class SettingsChangeRequestVoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


@router.post("/{slug}/service-request-settings-requests")
def create_service_request_settings_change_route(
    slug: str,
    payload: SettingsChangeRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_settings_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        reason=payload.reason,
        enabled=payload.enabled,
        request_mode=payload.request_mode,
        allow_off_schedule_requests=payload.allow_off_schedule_requests,
    )


@router.post("/{slug}/service-request-settings-requests/{request_id}/vote")
def vote_service_request_settings_change_route(
    slug: str,
    request_id: UUID,
    payload: SettingsChangeRequestVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_settings_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )
