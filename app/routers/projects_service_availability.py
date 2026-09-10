from __future__ import annotations

from datetime import date, time
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects.helpers import _get_project_by_slug_row
from app.services.projects_service_availability import (
    create_availability_rule,
    delete_availability_rule,
    list_availability_rules,
)

router = APIRouter(prefix="/projects", tags=["projects-service-availability"])


class AvailabilityRuleCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    timezone: str = Field(default="UTC", max_length=64)
    starts_on: date | None = None
    ends_on: date | None = None
    note: str = ""


@router.get("/{slug}/service-availability")
def list_project_availability_rules(
    slug: str,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _ = current_user_id
    project_row = _get_project_by_slug_row(db, slug)
    return {"items": list_availability_rules(db, project_row["id"])}


@router.post("/{slug}/service-availability")
def create_project_availability_rule_route(
    slug: str,
    payload: AvailabilityRuleCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_availability_rule(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        timezone=payload.timezone,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        note=payload.note,
    )


@router.delete("/{slug}/service-availability/{rule_id}")
def delete_project_availability_rule_route(
    slug: str,
    rule_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return delete_availability_rule(db, current_user_id, slug, rule_id)
