from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any

from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"


def create_access_token(subject: str, expires_delta: timedelta | None = None, extra_claims: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + (expires_delta or timedelta(minutes=settings.jwt_expire_minutes))
    payload: dict[str, Any] = {
        "sub": subject,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def verify_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])


def get_access_token_payload(token: str) -> dict[str, Any]:
    return verify_access_token(token)
