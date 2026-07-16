from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.cookies import (
    REFRESH_COOKIE,
    access_cookie_max_age_seconds,
    clear_auth_cookies,
    refresh_cookie_max_age_seconds,
    set_auth_cookies,
)
from app.auth.dependencies import get_current_user_token, resolve_refresh_token
from app.dependencies import get_db
from app.services.auth import (
    authenticate_user,
    enforce_auth_rate_limit,
    logout_refresh_token,
    logout_user,
    public_auth_payload,
    refresh_auth_session,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    profile_bio: str | None = Field(default=None, max_length=500)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AuthUser(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profile_image_url: str | None = None
    is_active: bool


class AuthResponse(BaseModel):
    access_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    user: AuthUser
    refresh_token: str | None = None
    csrf_token: str | None = None


class RefreshResponse(BaseModel):
    access_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    refresh_token: str | None = None
    csrf_token: str | None = None


class LogoutResponse(BaseModel):
    ok: bool = True


def _apply_auth_cookies(response: Response, payload: dict[str, object]) -> None:
    refresh_token = payload.get("refresh_token")
    csrf_token = payload.get("csrf_token")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str):
        return
    if not isinstance(refresh_token, str) or not isinstance(csrf_token, str):
        return

    set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        csrf_token=csrf_token,
        access_max_age_seconds=access_cookie_max_age_seconds(),
        refresh_max_age_seconds=refresh_cookie_max_age_seconds(),
    )


@router.post(
    "/register", response_model=AuthResponse, dependencies=[Depends(enforce_auth_rate_limit)]
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    auth_payload = register_user(db, payload.username, payload.password, payload.profile_bio)
    _apply_auth_cookies(response, auth_payload)
    return public_auth_payload(request, auth_payload)


@router.post("/login", response_model=AuthResponse, dependencies=[Depends(enforce_auth_rate_limit)])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    auth_payload = authenticate_user(db, payload.username, payload.password)
    _apply_auth_cookies(response, auth_payload)
    return public_auth_payload(request, auth_payload)


@router.post(
    "/refresh", response_model=RefreshResponse, dependencies=[Depends(enforce_auth_rate_limit)]
)
async def refresh(request: Request, response: Response) -> dict[str, object]:
    refresh_token = resolve_refresh_token(request)
    auth_payload = await refresh_auth_session(refresh_token)
    _apply_auth_cookies(response, auth_payload)
    return public_auth_payload(request, auth_payload)


@router.post(
    "/logout", response_model=LogoutResponse, dependencies=[Depends(enforce_auth_rate_limit)]
)
async def logout(
    request: Request,
    response: Response,
    token: str = Depends(get_current_user_token),
) -> dict[str, bool]:
    result = await logout_user(token)
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if refresh_token:
        try:
            await logout_refresh_token(refresh_token)
        except Exception:
            pass
    clear_auth_cookies(response)
    return result
