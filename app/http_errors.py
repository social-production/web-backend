"""Map domain errors to HTTP responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import (
    AppError,
    ConflictAppError,
    ForbiddenAppError,
    InternalAppError,
    NotFoundAppError,
    RateLimitedAppError,
    ServiceUnavailableAppError,
    UnauthorizedAppError,
    ValidationAppError,
)

_STATUS_BY_TYPE: dict[type[AppError], int] = {
    ValidationAppError: 422,
    NotFoundAppError: 404,
    UnauthorizedAppError: 401,
    ForbiddenAppError: 403,
    ConflictAppError: 409,
    RateLimitedAppError: 429,
    ServiceUnavailableAppError: 503,
    InternalAppError: 500,
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        status_code = 400
        for error_type, code in _STATUS_BY_TYPE.items():
            if isinstance(exc, error_type):
                status_code = code
                break
        return JSONResponse(status_code=status_code, content={"detail": exc.detail})
