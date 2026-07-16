from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import Response

from app.config import get_settings

ACCESS_COOKIE = "sp_access"
REFRESH_COOKIE = "sp_refresh"
CSRF_COOKIE = "sp_csrf"


def _cookie_secure() -> bool:
    return get_settings().is_production


def set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    csrf_token: str,
    access_max_age_seconds: int,
    refresh_max_age_seconds: int,
) -> None:
    secure = _cookie_secure()
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=access_max_age_seconds,
        path="/",
    )
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=refresh_max_age_seconds,
        path="/auth",
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=refresh_max_age_seconds,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    secure = _cookie_secure()
    for key, path in ((ACCESS_COOKIE, "/"), (REFRESH_COOKIE, "/auth"), (CSRF_COOKIE, "/")):
        response.set_cookie(
            key=key,
            value="",
            httponly=key != CSRF_COOKIE,
            secure=secure,
            samesite="lax",
            max_age=0,
            path=path,
        )


def access_cookie_max_age_seconds() -> int:
    settings = get_settings()
    return int(timedelta(minutes=settings.jwt_access_expire_minutes).total_seconds())


def refresh_cookie_max_age_seconds() -> int:
    settings = get_settings()
    return int(timedelta(days=settings.jwt_refresh_expire_days).total_seconds())


def utc_now() -> datetime:
    return datetime.now(UTC)
