from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import get_redis_client
from app.config import Settings, get_settings
from app.models import users
from app.utils.request import get_client_ip

FEEDBACK_RATE_LIMIT = 5
FEEDBACK_RATE_LIMIT_WINDOW_SECONDS = 3600
FEEDBACK_RATE_LIMIT_PREFIX = "feedback-rate-limit"

VALID_CATEGORIES = {"bug", "idea"}


async def enforce_feedback_rate_limit(request: Request) -> None:
    client_host = get_client_ip(request)
    window_bucket = int(time.time() // FEEDBACK_RATE_LIMIT_WINDOW_SECONDS)
    key = f"{FEEDBACK_RATE_LIMIT_PREFIX}:{client_host}:{window_bucket}"

    redis_client: Redis = get_redis_client()
    try:
        current_count = await redis_client.incr(key)
        if current_count == 1:
            await redis_client.expire(key, FEEDBACK_RATE_LIMIT_WINDOW_SECONDS + 1)
    except Exception:
        if get_settings().rate_limit_fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting service temporarily unavailable",
            )
        return

    if current_count > FEEDBACK_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="feedback_rate_limit_exceeded",
        )


def _resolve_submitter_username(db: Session, user_id: UUID | None) -> str | None:
    if user_id is None:
        return None

    row = db.execute(select(users.c.username).where(users.c.id == user_id)).first()
    return row[0] if row else None


def _build_issue_body(
    *,
    description: str,
    page_url: str | None,
    user_agent: str | None,
    submitter_username: str | None,
) -> str:
    lines = [description.strip(), "", "---", ""]

    if page_url:
        lines.append(f"**Page:** {page_url}")
    if submitter_username:
        lines.append(f"**Submitted by:** @{submitter_username}")
    else:
        lines.append("**Submitted by:** guest")
    if user_agent:
        lines.append(f"**User agent:** {user_agent}")

    lines.append(f"**Submitted at:** {datetime.now(timezone.utc).isoformat()}")
    return "\n".join(lines)


async def create_github_feedback_issue(
    db: Session,
    *,
    category: str,
    title: str,
    description: str,
    page_url: str | None,
    user_agent: str | None,
    submitter_user_id: UUID | None,
    settings: Settings | None = None,
) -> dict[str, object]:
    safe_category = category.strip().lower()
    if safe_category not in VALID_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_feedback_category")

    safe_title = title.strip()
    safe_description = description.strip()
    if not safe_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="feedback_title_required")
    if not safe_description:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="feedback_description_required")

    runtime_settings = settings or get_settings()
    token = runtime_settings.github_token.strip()
    repo = runtime_settings.github_repo.strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="feedback_not_configured",
        )
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="feedback_not_configured",
        )

    label = "bug" if safe_category == "bug" else "enhancement"
    prefix = "[Bug]" if safe_category == "bug" else "[Idea]"
    issue_title = f"{prefix} {safe_title}"[:240]
    submitter_username = _resolve_submitter_username(db, submitter_user_id)
    issue_body = _build_issue_body(
        description=safe_description,
        page_url=page_url.strip() if page_url else None,
        user_agent=user_agent.strip() if user_agent else None,
        submitter_username=submitter_username,
    )

    api_url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": issue_title, "body": issue_body, "labels": [label]}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(api_url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="feedback_submission_failed",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="feedback_submission_failed",
        )

    data = response.json()
    issue_number = data.get("number")
    issue_url = data.get("html_url")
    if not isinstance(issue_number, int) or not isinstance(issue_url, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="feedback_submission_failed",
        )

    return {"issue_number": issue_number, "issue_url": issue_url}
