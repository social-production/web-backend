from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.cookies import ACCESS_COOKIE, REFRESH_COOKIE
from app.auth.jwt import JWTError, get_access_token_payload
from app.cache import get_redis_client

bearer_scheme = HTTPBearer(auto_error=False)

TOKEN_BLACKLIST_PREFIX = "token-blacklist"


def resolve_access_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is not None and credentials.credentials:
        return credentials.credentials

    cookie_token = request.cookies.get(ACCESS_COOKIE)
    if cookie_token:
        return cookie_token

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def resolve_refresh_token(request: Request) -> str:
    cookie_token = request.cookies.get(REFRESH_COOKIE)
    if cookie_token:
        return cookie_token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def get_current_user_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    return resolve_access_token(request, credentials)


async def _is_blacklisted_jti(jti: str | None) -> bool:
    if not jti:
        return False

    redis_client = get_redis_client()
    try:
        return await redis_client.exists(f"{TOKEN_BLACKLIST_PREFIX}:{jti}") > 0
    except Exception:
        from app.config import get_settings

        if get_settings().rate_limit_fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable",
            )
        return False


async def get_current_user_token_payload(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, object]:
    token = resolve_access_token(request, credentials)
    try:
        payload = get_access_token_payload(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if await _is_blacklisted_jti(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked"
        )

    return payload


async def get_current_user_id(
    payload: dict[str, object] = Depends(get_current_user_token_payload),
) -> UUID:
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        )

    try:
        return UUID(subject)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject"
        ) from exc


async def get_optional_current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UUID | None:
    token: str | None = None
    if credentials is not None and credentials.credentials:
        token = credentials.credentials
    elif request.cookies.get(ACCESS_COOKIE):
        token = request.cookies.get(ACCESS_COOKIE)

    if token is None:
        return None

    try:
        payload = get_access_token_payload(token)
    except JWTError:
        return None

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        return None

    try:
        if await _is_blacklisted_jti(jti):
            return None
    except HTTPException:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None

    try:
        return UUID(subject)
    except ValueError:
        return None
