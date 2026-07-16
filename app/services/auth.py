from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    get_access_token_payload,
    verify_refresh_token,
)
from app.auth.passwords import hash_password, verify_password
from app.cache import get_redis_client
from app.config import get_settings
from app.models import user_settings, users
from app.services.search import index_document
from app.utils.request import get_client_ip

AUTH_RATE_LIMIT = 10
AUTH_RATE_LIMIT_WINDOW_SECONDS = 60
AUTH_RATE_LIMIT_PREFIX = "auth-rate-limit"
TOKEN_BLACKLIST_PREFIX = "token-blacklist"


def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _serialize_user(user_row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": user_row["id"],
        "username": user_row["username"],
        "bio": user_row["bio"],
        "profile_image_url": user_row["profile_image_url"],
        "is_active": user_row["is_active"],
    }


def _build_auth_response(
    user_row: Mapping[str, object],
    access_token: str,
    *,
    refresh_token: str | None = None,
    csrf_token: str | None = None,
) -> dict[str, object]:
    response = {
        "access_token": access_token,
        "token_type": "bearer",
        "user": _serialize_user(user_row),
    }
    if refresh_token is not None:
        response["refresh_token"] = refresh_token
    if csrf_token is not None:
        response["csrf_token"] = csrf_token
    return response


def _issue_auth_bundle(user_row: Mapping[str, object]) -> dict[str, object]:
    user_id = str(user_row["id"])
    access_token = create_access_token(user_id)
    refresh_token = create_refresh_token(user_id)
    csrf_token = secrets.token_urlsafe(32)
    return _build_auth_response(
        user_row,
        access_token,
        refresh_token=refresh_token,
        csrf_token=csrf_token,
    )


def _include_tokens_in_response(request: Request) -> bool:
    return request.headers.get("x-include-tokens", "").lower() in {"1", "true", "yes"}


def public_auth_payload(request: Request, payload: dict[str, object]) -> dict[str, object]:
    if _include_tokens_in_response(request):
        return payload
    if "user" in payload:
        return {
            "token_type": payload.get("token_type", "bearer"),
            "user": payload["user"],
        }
    return {"token_type": payload.get("token_type", "bearer")}


async def enforce_auth_rate_limit(request: Request) -> None:
    client_host = get_client_ip(request)
    window_bucket = int(time.time() // AUTH_RATE_LIMIT_WINDOW_SECONDS)
    key = f"{AUTH_RATE_LIMIT_PREFIX}:{client_host}:{window_bucket}"

    redis_client: Redis = get_redis_client()
    try:
        current_count = await redis_client.incr(key)
        if current_count == 1:
            await redis_client.expire(key, AUTH_RATE_LIMIT_WINDOW_SECONDS + 1)
    except Exception:
        if get_settings().rate_limit_fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting service temporarily unavailable",
            )
        return

    if current_count > AUTH_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Auth rate limit exceeded"
        )


def register_user(
    db: Session, username: str, password: str, profile_bio: str | None = None
) -> dict[str, object]:
    normalized_username = _normalize_username(username)
    if not normalized_username:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Username is required"
        )
    if not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Password is required"
        )

    existing_user = db.execute(
        select(users.c.id).where(users.c.username == normalized_username)
    ).first()
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    try:
        created_user = (
            db.execute(
                insert(users)
                .values(
                    username=normalized_username,
                    password_hash=hash_password(password),
                    bio=profile_bio,
                )
                .returning(
                    users.c.id,
                    users.c.username,
                    users.c.bio,
                    users.c.profile_image_url,
                    users.c.is_active,
                )
            )
            .mappings()
            .one()
        )
        db.execute(insert(user_settings).values(user_id=created_user["id"]))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Username already exists"
        ) from exc

    index_document(
        db=db,
        entity_type="user",
        entity_id=created_user["id"],
        title=created_user["username"],
        summary=created_user["bio"] if created_user["bio"] else created_user["username"],
        meta="user",
        href=f"/profile/{created_user['username']}",
    )
    return _issue_auth_bundle(created_user)


def authenticate_user(db: Session, username: str, password: str) -> dict[str, object]:
    normalized_username = _normalize_username(username)
    if not normalized_username or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    user_row = (
        db.execute(
            select(
                users.c.id,
                users.c.username,
                users.c.bio,
                users.c.profile_image_url,
                users.c.is_active,
                users.c.password_hash,
            ).where(users.c.username == normalized_username)
        )
        .mappings()
        .first()
    )

    if user_row is None or not user_row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    try:
        password_matches = verify_password(password, user_row["password_hash"])
    except ValueError:
        password_matches = False

    if not password_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    return _issue_auth_bundle(user_row)


async def refresh_auth_session(refresh_token: str) -> dict[str, object]:
    try:
        payload = verify_refresh_token(refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    jti = payload.get("jti")
    exp = payload.get("exp")
    subject = payload.get("sub")
    if (
        not isinstance(jti, str)
        or not jti
        or not isinstance(exp, int)
        or not isinstance(subject, str)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    if await _is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token has been revoked"
        )

    await _blacklist_token(jti, exp)

    access_token = create_access_token(subject)
    rotated_refresh = create_refresh_token(subject)
    csrf_token = secrets.token_urlsafe(32)
    return {
        "access_token": access_token,
        "refresh_token": rotated_refresh,
        "csrf_token": csrf_token,
        "token_type": "bearer",
    }


async def _blacklist_token(jti: str, exp: int) -> None:
    ttl_seconds = exp - int(datetime.now(UTC).timestamp())
    if ttl_seconds <= 0:
        return
    redis_client: Redis = get_redis_client()
    await redis_client.set(f"{TOKEN_BLACKLIST_PREFIX}:{jti}", "1", ex=ttl_seconds)


async def _is_token_blacklisted(jti: str) -> bool:
    redis_client: Redis = get_redis_client()
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


async def logout_user(token: str) -> dict[str, bool]:
    try:
        payload = get_access_token_payload(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    jti = payload.get("jti")
    exp = payload.get("exp")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if not isinstance(exp, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    ttl_seconds = exp - int(datetime.now(UTC).timestamp())
    if ttl_seconds > 0:
        await _blacklist_token(jti, exp)

    return {"ok": True}


async def logout_refresh_token(refresh_token: str) -> dict[str, bool]:
    try:
        payload = verify_refresh_token(refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    jti = payload.get("jti")
    exp = payload.get("exp")
    if not isinstance(jti, str) or not jti or not isinstance(exp, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    await _blacklist_token(jti, exp)
    return {"ok": True}
