from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.events_plans import (
    cast_event_plan_value_vote,
    cast_event_plan_vote,
    list_event_plans,
    submit_event_plan,
)

router = APIRouter(prefix="/events", tags=["events-plans"])


class EventPlanSubmitRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    demand_consideration_note: str = Field(default="")
    location_label: str = Field(min_length=1, max_length=160)
    schedule_payload: dict[str, object] = Field(default_factory=dict)
    plan_payload: dict[str, object] = Field(default_factory=dict)


class EventPlanVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no|neutral)$")


class EventPlanValueVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    value_id: UUID
    vote: str = Field(pattern="^(yes|no|neutral)$")


class EventPlanVoteSummaryOut(BaseModel):
    yes_count: int
    no_count: int
    total_votes: int
    approval_ratio: float
    approval_threshold: float
    votes_required: int
    member_count: int
    meets_quorum: bool
    meets_approval: bool
    is_winning: bool


class EventPlanOut(BaseModel):
    id: UUID
    event_id: UUID
    title: str
    description: str
    author_id: UUID | None = None
    demand_consideration_note: str
    location_label: str
    schedule_payload: dict[str, object]
    plan_payload: dict[str, object]
    is_leading: bool
    status: str
    created_at: object
    vote_summary: EventPlanVoteSummaryOut


class EventPlanResponse(BaseModel):
    plan: EventPlanOut


class EventPlansListResponse(BaseModel):
    event_slug: str
    total: int
    items: list[EventPlanOut]


class EventPlanVoteResponse(BaseModel):
    plan: EventPlanOut
    vote: str
    is_leading: bool


@router.post("/{slug}/plans", response_model=EventPlanResponse)
def submit_plan(
    slug: str,
    payload: EventPlanSubmitRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return submit_event_plan(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        title=payload.title,
        description=payload.description,
        demand_consideration_note=payload.demand_consideration_note,
        location_label=payload.location_label,
        schedule_payload=payload.schedule_payload,
        plan_payload=payload.plan_payload,
    )


@router.get("/{slug}/plans", response_model=EventPlansListResponse)
def get_plans(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_event_plans(db=db, event_slug=slug)


@router.post("/{slug}/plans/{plan_id}/vote", response_model=EventPlanVoteResponse)
def vote_plan(
    slug: str,
    plan_id: UUID,
    payload: EventPlanVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_event_plan_vote(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        plan_id=plan_id,
        vote=payload.vote,
    )


@router.post("/{slug}/plans/{plan_id}/value-votes")
def vote_plan_value(
    slug: str,
    plan_id: UUID,
    payload: EventPlanValueVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_event_plan_value_vote(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        plan_id=plan_id,
        value_id=payload.value_id,
        vote=payload.vote,
    )
