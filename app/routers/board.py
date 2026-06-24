from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.board import cast_standing_vote, list_board_standing, remove_volunteer, volunteer_as_candidate

router = APIRouter(prefix="/board", tags=["board"])


class BoardProfileOut(BaseModel):
    user_id: UUID
    username: str
    standing_state: str
    updated_at: object
    yes_count: int
    no_count: int
    vote_count: int
    approval_ratio: float
    required_quorum: int = 0
    weekly_active_users: int = 0
    active_vote: str | None = None


class VolunteerResponse(BaseModel):
    candidate: BoardProfileOut
    weekly_active_users: int
    required_quorum: int
    removed_member_ids: list[UUID]


class StandingVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_user_id: UUID
    vote: str = Field(pattern="^(yes|no|neutral)$")


class StandingVoteResponse(BaseModel):
    target_user_id: UUID
    vote: str
    yes_count: int
    no_count: int
    vote_count: int
    approval_ratio: float
    weekly_active_users: int
    required_quorum: int
    removed_member_ids: list[UUID]


class BoardStandingListResponse(BaseModel):
    weekly_active_users: int
    required_quorum: int
    members: list[BoardProfileOut]
    candidates: list[BoardProfileOut]
    total_members: int
    total_candidates: int
    removed_member_ids: list[UUID]


@router.post("/volunteer", response_model=VolunteerResponse)
def volunteer(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return volunteer_as_candidate(db=db, current_user_id=current_user_id)


@router.delete("/volunteer", response_model=dict)
def unvolunteer(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return remove_volunteer(db=db, current_user_id=current_user_id)


@router.post("/votes", response_model=StandingVoteResponse)
def cast_vote(
    payload: StandingVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_standing_vote(
        db=db,
        current_user_id=current_user_id,
        target_user_id=payload.target_user_id,
        vote=payload.vote,
    )


@router.get("", response_model=BoardStandingListResponse)
def list_board(
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_board_standing(db=db, viewer_user_id=current_user_id)
