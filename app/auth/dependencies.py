from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from app.auth.jwt import get_access_token_payload
from app.cache import get_redis_client

bearer_scheme = HTTPBearer(auto_error=False)

TOKEN_BLACKLIST_PREFIX = "token-blacklist"


def get_current_user_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> str:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return credentials.credentials


async def _is_blacklisted_jti(jti: str | None) -> bool:
    if not jti:
        return False

    redis_client = get_redis_client()
    return await redis_client.exists(f"{TOKEN_BLACKLIST_PREFIX}:{jti}") > 0


async def get_current_user_token_payload(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict[str, object]:
    token = get_current_user_token(credentials)
    try:
        payload = get_access_token_payload(token)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if await _is_blacklisted_jti(jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")

    return payload


async def get_current_user_id(payload: dict[str, object] = Depends(get_current_user_token_payload)) -> UUID:
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    try:
        return UUID(subject)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject") from exc