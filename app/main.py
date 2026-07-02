from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.cache import close_redis_client
from app.config import get_settings
from app.dependencies import get_cache
from app.db import engine
from app.routers.auth import router as auth_router
from app.routers.board import router as board_router
from app.routers.bootstrap import router as bootstrap_router
from app.routers.content import router as content_router
from app.routers.events import router as events_router
from app.routers.events_phases import router as events_phases_router
from app.routers.events_plans import router as events_plans_router
from app.routers.governance import router as governance_router
from app.routers.messages import router as messages_router
from app.routers.notifications import router as notifications_router
from app.routers.projects import router as projects_router
from app.routers.projects_links import router as projects_links_router
from app.routers.projects_phases import router as projects_phases_router
from app.routers.projects_plans import router as projects_plans_router
from app.routers.projects_service_requests import router as projects_service_requests_router
from app.routers.projects_service_request_settings import router as projects_service_request_settings_router
from app.routers.projects_software import router as projects_software_router
from app.routers.feedback import router as feedback_router
from app.routers.feeds import router as feeds_router
from app.routers.platform import router as platform_router
from app.routers.search import router as search_router
from app.routers.scopes import router as scopes_router
from app.routers.users import router as users_router
from app.middleware.rate_limit import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_redis_client()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_runtime_settings()
    docs_url = None if settings.is_production and settings.disable_openapi_in_production else "/docs"
    openapi_url = None if settings.is_production and settings.disable_openapi_in_production else "/openapi.json"
    app = FastAPI(
        title="Social Production Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.allow_cors_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            with engine.connect() as connection:
                connection.execute(text("select 1"))
            await get_cache().ping()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dependencies are not ready",
            ) from exc
        return {"status": "ready"}

    app.include_router(bootstrap_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(scopes_router)
    app.include_router(content_router)
    app.include_router(governance_router)
    app.include_router(board_router)
    app.include_router(messages_router)
    app.include_router(notifications_router)
    app.include_router(events_router)
    app.include_router(events_plans_router)
    app.include_router(events_phases_router)
    app.include_router(search_router)
    app.include_router(feeds_router)
    app.include_router(feedback_router)
    app.include_router(platform_router)
    app.include_router(projects_router)
    app.include_router(projects_links_router)
    app.include_router(projects_plans_router)
    app.include_router(projects_phases_router)
    app.include_router(projects_service_requests_router)
    app.include_router(projects_service_request_settings_router)
    app.include_router(projects_software_router)

    return app


app = create_app()
