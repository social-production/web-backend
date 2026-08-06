from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.adapters.postgres.access_policy import PostgresAccessPolicy
from app.adapters.postgres.auth import JwtAuthProvider
from app.adapters.postgres.feeds import PostgresFeedProvider
from app.adapters.postgres.messaging import PostgresMessagingProvider
from app.adapters.postgres.notifications import PostgresNotificationsProvider
from app.adapters.postgres.people_suggestions import PostgresPeopleSuggestionsProvider
from app.adapters.postgres.search import PostgresSearchProvider
from app.adapters.redis.cache_store import RedisCacheStore
from app.adapters.redis.token_revocation import get_token_revocation_store
from app.cache import get_redis_client
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.ports import (
    AccessPolicy,
    AuthProvider,
    CacheStore,
    FeedProvider,
    MessagingProvider,
    NotificationsProvider,
    PeopleSuggestionsProvider,
    SearchProvider,
    TokenRevocationStore,
)


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


def get_cache_store() -> CacheStore:
    return RedisCacheStore()


def get_token_store() -> TokenRevocationStore:
    return get_token_revocation_store()


def get_search_provider(db: Session = Depends(get_db)) -> SearchProvider:
    return PostgresSearchProvider(db)


def get_access_policy(db: Session = Depends(get_db)) -> AccessPolicy:
    return PostgresAccessPolicy(db)


def get_auth_provider(db: Session = Depends(get_db)) -> AuthProvider:
    return JwtAuthProvider(db)


def get_people_suggestions_provider(
    db: Session = Depends(get_db),
) -> PeopleSuggestionsProvider:
    return PostgresPeopleSuggestionsProvider(db)


def get_notifications_provider(db: Session = Depends(get_db)) -> NotificationsProvider:
    return PostgresNotificationsProvider(db)


def get_messaging_provider(db: Session = Depends(get_db)) -> MessagingProvider:
    return PostgresMessagingProvider(db)


def get_feed_provider(db: Session = Depends(get_db)) -> FeedProvider:
    return PostgresFeedProvider(db)
