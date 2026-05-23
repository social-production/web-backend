from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_token
from app.dependencies import get_db
from app.services.auth import authenticate_user, enforce_auth_rate_limit, logout_user, register_user

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
    access_token: str
    token_type: Literal["bearer"]
    user: AuthUser


class LogoutResponse(BaseModel):
    ok: bool = True


@router.post("/register", response_model=AuthResponse, dependencies=[Depends(enforce_auth_rate_limit)])
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    return register_user(db, payload.username, payload.password, payload.profile_bio)


@router.post("/login", response_model=AuthResponse, dependencies=[Depends(enforce_auth_rate_limit)])
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    return authenticate_user(db, payload.username, payload.password)


@router.post("/logout", response_model=LogoutResponse, dependencies=[Depends(enforce_auth_rate_limit)])
async def logout(token: str = Depends(get_current_user_token)) -> dict[str, bool]:
    return await logout_user(token)
