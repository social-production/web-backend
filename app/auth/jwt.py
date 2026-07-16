from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"
TokenKind = Literal["access", "refresh"]


def create_access_token(
    subject: str, expires_delta: timedelta | None = None, extra_claims: dict[str, Any] | None = None
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + (expires_delta or timedelta(minutes=settings.jwt_access_expire_minutes))
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": str(uuid4()),
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_refresh_token(subject: str, expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + (expires_delta or timedelta(days=settings.jwt_refresh_expire_days))
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": str(uuid4()),
        "typ": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def verify_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("typ") not in (None, "access"):
        raise JWTError("Invalid access token type")
    return payload


def verify_refresh_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("typ") != "refresh":
        raise JWTError("Invalid refresh token type")
    return payload


def get_access_token_payload(token: str) -> dict[str, Any]:
    return verify_access_token(token)
