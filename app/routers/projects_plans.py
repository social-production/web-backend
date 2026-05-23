from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_plans import cast_project_plan_vote, list_project_plans, submit_project_plan

router = APIRouter(prefix="/projects", tags=["projects-plans"])


class ProjectPlanSubmitRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    plan_type: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    demand_consideration_note: str = Field(default="")
    total_cost_label: str | None = Field(default=None, max_length=80)
    repository_url: str | None = None
    plan_payload: dict[str, object] = Field(default_factory=dict)


class ProjectPlanVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class ProjectPlanVoteSummaryOut(BaseModel):
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


class ProjectPlanOut(BaseModel):
    id: UUID
    project_id: UUID
    phase_kind: str
    title: str
    description: str
    author_id: UUID | None = None
    project_subtype: str | None = None
    repository_url: str | None = None
    demand_consideration_note: str
    total_cost_label: str | None = None
    plan_payload: dict[str, object]
    is_leading: bool
    status: str
    created_at: object
    updated_at: object
    vote_summary: ProjectPlanVoteSummaryOut


class ProjectPlanResponse(BaseModel):
    plan: ProjectPlanOut


class ProjectPlansListResponse(BaseModel):
    project_slug: str
    project_mode: str
    total: int
    items: list[ProjectPlanOut]


class ProjectPlanVoteResponse(BaseModel):
    plan: ProjectPlanOut
    vote: str
    is_leading: bool


@router.post("/{slug}/plans", response_model=ProjectPlanResponse)
def submit_plan(
    slug: str,
    payload: ProjectPlanSubmitRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return submit_project_plan(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        plan_type=payload.plan_type,
        title=payload.title,
        description=payload.description,
        demand_consideration_note=payload.demand_consideration_note,
        total_cost_label=payload.total_cost_label,
        repository_url=payload.repository_url,
        plan_payload=payload.plan_payload,
    )


@router.get("/{slug}/plans", response_model=ProjectPlansListResponse)
def get_project_plans(
    slug: str,
    plan_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_project_plans(db=db, project_slug=slug, plan_type=plan_type)


@router.post("/{slug}/plans/{plan_id}/vote", response_model=ProjectPlanVoteResponse)
def vote_project_plan(
    slug: str,
    plan_id: UUID,
    payload: ProjectPlanVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_project_plan_vote(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        plan_id=plan_id,
        vote=payload.vote,
    )
