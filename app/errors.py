"""Domain/application errors — transport-agnostic.

Routers and the global exception handler map these to HTTP responses.
Services should raise these instead of FastAPI ``HTTPException``.
"""

from __future__ import annotations


class AppError(Exception):
    """Base application error."""

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


class ValidationAppError(AppError):
    """Invalid input / unprocessable entity (HTTP 422)."""


class NotFoundAppError(AppError):
    """Resource missing or hidden (HTTP 404)."""


class UnauthorizedAppError(AppError):
    """Authentication required or failed (HTTP 401)."""


class ForbiddenAppError(AppError):
    """Authenticated but not allowed (HTTP 403)."""


class ConflictAppError(AppError):
    """State conflict (HTTP 409)."""


class RateLimitedAppError(AppError):
    """Too many requests (HTTP 429)."""


class ServiceUnavailableAppError(AppError):
    """Dependency unavailable (HTTP 503)."""


class InternalAppError(AppError):
    """Unexpected failure (HTTP 500)."""
