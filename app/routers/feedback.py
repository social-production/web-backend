from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_optional_current_user_id
from app.dependencies import get_db
from app.services.feedback import create_github_feedback_issue, enforce_feedback_rate_limit

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    page_url: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=200)


class FeedbackSubmitResponse(BaseModel):
    issue_number: int
    issue_url: str


@router.post("", response_model=FeedbackSubmitResponse)
async def submit_feedback(
    payload: FeedbackSubmitRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    if payload.website:
        return {"issue_number": 0, "issue_url": ""}

    await enforce_feedback_rate_limit(request)
    user_agent = request.headers.get("user-agent")

    return await create_github_feedback_issue(
        db,
        category=payload.category,
        title=payload.title,
        description=payload.description,
        page_url=payload.page_url,
        user_agent=user_agent,
        submitter_user_id=current_user_id,
    )
