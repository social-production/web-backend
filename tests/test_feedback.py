from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.services.feedback import VALID_CATEGORIES, create_github_feedback_issue


def test_valid_feedback_categories() -> None:
    assert VALID_CATEGORIES == {"bug", "idea"}


def test_create_github_feedback_issue_rejects_invalid_category() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            create_github_feedback_issue(
                db=None,  # type: ignore[arg-type]
                category="invalid",
                title="Title",
                description="Body",
                page_url=None,
                user_agent=None,
                submitter_user_id=None,
            )
        )

    assert exc.value.status_code == 422


def test_create_github_feedback_issue_requires_github_config() -> None:
    settings = Settings(github_token="", github_repo="")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            create_github_feedback_issue(
                db=None,  # type: ignore[arg-type]
                category="bug",
                title="Broken button",
                description="It does not click",
                page_url="/feedback",
                user_agent="test",
                submitter_user_id=None,
                settings=settings,
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "feedback_not_configured"
