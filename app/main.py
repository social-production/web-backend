from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.cache import close_redis_client
from app.config import get_settings
from app.routers.auth import router as auth_router
from app.routers.content import router as content_router
from app.routers.governance import router as governance_router
from app.routers.projects import router as projects_router
from app.routers.projects_phases import router as projects_phases_router
from app.routers.projects_plans import router as projects_plans_router
from app.routers.projects_service_requests import router as projects_service_requests_router
from app.routers.scopes import router as scopes_router
from app.routers.users import router as users_router
from app.middleware.rate_limit import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_redis_client()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Social Production Backend", version="0.1.0", lifespan=lifespan)

    if settings.cors_origins.strip() == "*":
        allow_origins = ["*"]
    else:
        allow_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(scopes_router)
    app.include_router(content_router)
    app.include_router(governance_router)
    app.include_router(projects_router)
    app.include_router(projects_plans_router)
    app.include_router(projects_phases_router)
    app.include_router(projects_service_requests_router)

    return app


app = create_app()
