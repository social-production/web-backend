from __future__ import annotations

from collections.abc import Generator

from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.cache import get_redis_client
from app.config import Settings, get_settings
from app.db import SessionLocal


def get_app_settings() -> Settings:
    return get_settings()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_cache() -> Redis:
    return get_redis_client()
