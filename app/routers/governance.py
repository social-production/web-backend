from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.governance import add_comment, cast_vote, get_comments

router = APIRouter(prefix="/governance", tags=["governance"])


class CommentCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject_type: str = Field(pattern="^(thread|post)$")
    subject_id: UUID
    body: str = Field(min_length=1)
    parent_id: UUID | None = None


class VoteCastRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_type: str = Field(pattern="^(thread|post|comment)$")
    target_id: UUID
    direction: str = Field(pattern="^(up|down|neutral)$")


class CommentOut(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    parent_id: UUID | None = None
    author_id: UUID | None = None
    body: str
    vote_count: int
    created_at: object
    updated_at: object
    replies: list["CommentOut"] = Field(default_factory=list)


class CommentCreateResponse(BaseModel):
    comment: CommentOut


class CommentsListResponse(BaseModel):
    subject_type: str
    subject_id: UUID
    total: int
    items: list[CommentOut]


class VoteCastResponse(BaseModel):
    target_type: str
    target_id: UUID
    direction: str
    value: int


@router.post("/comments", response_model=CommentCreateResponse)
def create_comment(
    payload: CommentCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_comment(
        db=db,
        current_user_id=current_user_id,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        body=payload.body,
        parent_id=payload.parent_id,
    )


@router.get("/comments", response_model=CommentsListResponse)
def list_comments(
    subject_type: str = Query(pattern="^(thread|post)$"),
    subject_id: UUID = Query(),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_comments(db=db, subject_type=subject_type, subject_id=subject_id)


@router.post("/votes", response_model=VoteCastResponse)
def create_vote(
    payload: VoteCastRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_vote(
        db=db,
        current_user_id=current_user_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        direction=payload.direction,
    )
