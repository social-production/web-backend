from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.content import create_feedback, get_feedback_by_id, list_feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    kind: str = Field(pattern="^(bug|suggestion)$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)


@router.post("")
def create_feedback_endpoint(
    payload: FeedbackCreateRequest,
    db: Session = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
) -> dict[str, object]:
    return create_feedback(
        db,
        current_user_id,
        kind=payload.kind,
        title=payload.title,
        description=payload.description,
    )


@router.get("")
def list_feedback_endpoint(
    filter: str = Query(default="all", pattern="^(all|bugs|suggestions)$"),
    sort: str = Query(default="trending", pattern="^(trending|recent)$"),
    kind: str | None = Query(default=None, pattern="^(bug|suggestion)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    filter_value = "bugs" if kind == "bug" else "suggestions" if kind == "suggestion" else filter
    return list_feedback(
        db,
        current_user_id=current_user_id,
        filter_value=filter_value,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/{feedback_id}")
def get_feedback_endpoint(
    feedback_id: UUID,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_feedback_by_id(db, feedback_id, current_user_id=current_user_id)
