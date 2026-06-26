from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_service_requests import (
    create_service_request,
    list_service_requests,
    plan_service_request,
    toggle_service_history_completion,
    update_service_request_status,
)

router = APIRouter(prefix="/projects", tags=["projects-service-requests"])


class ServiceRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None


class ServiceRequestStatusUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: str = Field(pattern="^(open|planned|accepted|declined)$")


class ServiceRequestOut(BaseModel):
    id: UUID
    project_id: UUID
    requester_id: UUID | None = None
    title: str
    body: str
    status: str
    scheduled_at: object
    ends_at: object
    linked_activity_id: UUID | None = None
    created_at: object
    updated_at: object


class ServiceRequestResponse(BaseModel):
    request: ServiceRequestOut
    conversation_id: UUID | None = None


class ServiceRequestListResponse(BaseModel):
    project_slug: str
    project_mode: str
    total: int
    items: list[ServiceRequestOut]


@router.post("/{slug}/service-requests", response_model=ServiceRequestResponse)
def create_project_service_request(
    slug: str,
    payload: ServiceRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_service_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        title=payload.title,
        body=payload.body,
        scheduled_at=payload.scheduled_at,
        ends_at=payload.ends_at,
    )


@router.get("/{slug}/service-requests", response_model=ServiceRequestListResponse)
def get_project_service_requests(
    slug: str,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_service_requests(db=db, project_slug=slug, status_filter=status)


@router.patch("/{slug}/service-requests/{request_id}", response_model=ServiceRequestResponse)
def patch_project_service_request_status(
    slug: str,
    request_id: UUID,
    payload: ServiceRequestStatusUpdateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return update_service_request_status(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        status_value=payload.status,
    )


class ServiceRequestPlanIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    location_label: str = Field(min_length=1, max_length=160)
    role_requirements: list[dict] = Field(default_factory=list)
    linked_plan_phase_id: str | None = None
    note: str = Field(min_length=1)


class ServiceHistoryCompletionIn(BaseModel):
    role: str = Field(pattern="^(requester|participants)$")
    selection: str | None = Field(default=None)


@router.post("/{slug}/service-requests/{request_id}/plan")
def plan_project_service_request_route(
    slug: str,
    request_id: UUID,
    payload: ServiceRequestPlanIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return plan_service_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        title=payload.title,
        location_label=payload.location_label,
        role_requirements=payload.role_requirements,
        linked_plan_phase_id=payload.linked_plan_phase_id,
        note=payload.note,
    )


@router.post("/{slug}/service-history/{history_id}/completion")
def toggle_service_history_completion_route(
    slug: str,
    history_id: str,
    payload: ServiceHistoryCompletionIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return toggle_service_history_completion(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        history_item_key=history_id,
        role=payload.role,
        selection=payload.selection,
    )
