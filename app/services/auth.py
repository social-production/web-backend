from __future__ import annotations

import time
from datetime import datetime, timezone
from collections.abc import Mapping

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.jwt import get_access_token_payload, create_access_token
from app.auth.passwords import hash_password, verify_password
from app.cache import get_redis_client
from app.models import user_settings, users
from app.services.search import index_document

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


def _build_auth_response(user_row: Mapping[str, object], access_token: str) -> dict[str, object]:
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": _serialize_user(user_row),
    }


async def enforce_auth_rate_limit(request: Request) -> None:
    client = request.client
    client_host = client.host if client and client.host else "unknown"
    window_bucket = int(time.time() // AUTH_RATE_LIMIT_WINDOW_SECONDS)
    key = f"{AUTH_RATE_LIMIT_PREFIX}:{client_host}:{window_bucket}"

    redis_client: Redis = get_redis_client()
    try:
        current_count = await redis_client.incr(key)
        if current_count == 1:
            await redis_client.expire(key, AUTH_RATE_LIMIT_WINDOW_SECONDS + 1)
    except Exception:
        return

    if current_count > AUTH_RATE_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Auth rate limit exceeded")


def register_user(db: Session, username: str, password: str, profile_bio: str | None = None) -> dict[str, object]:
    normalized_username = _normalize_username(username)
    if not normalized_username:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Username is required")
    if not password:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Password is required")

    existing_user = db.execute(select(users.c.id).where(users.c.username == normalized_username)).first()
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    try:
        created_user = db.execute(
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
        ).mappings().one()
        db.execute(insert(user_settings).values(user_id=created_user["id"]))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc

    index_document(
        db=db,
        entity_type="user",
        entity_id=created_user["id"],
        title=created_user["username"],
        summary=created_user["bio"] if created_user["bio"] else created_user["username"],
        meta="user",
        href=f"/users/{created_user['username']}",
    )
    access_token = create_access_token(str(created_user["id"]))
    return _build_auth_response(created_user, access_token)


def authenticate_user(db: Session, username: str, password: str) -> dict[str, object]:
    normalized_username = _normalize_username(username)
    if not normalized_username or not password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    user_row = db.execute(
        select(
            users.c.id,
            users.c.username,
            users.c.bio,
            users.c.profile_image_url,
            users.c.is_active,
            users.c.password_hash,
        ).where(users.c.username == normalized_username)
    ).mappings().first()

    if user_row is None or not user_row["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    try:
        password_matches = verify_password(password, user_row["password_hash"])
    except ValueError:
        password_matches = False

    if not password_matches:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    access_token = create_access_token(str(user_row["id"]))
    return _build_auth_response(user_row, access_token)


async def logout_user(token: str) -> dict[str, bool]:
    try:
        payload = get_access_token_payload(token)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    jti = payload.get("jti")
    exp = payload.get("exp")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if not isinstance(exp, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    ttl_seconds = exp - int(datetime.now(timezone.utc).timestamp())
    if ttl_seconds > 0:
        redis_client: Redis = get_redis_client()
        await redis_client.set(f"{TOKEN_BLACKLIST_PREFIX}:{jti}", "1", ex=ttl_seconds)

    return {"ok": True}
