from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


FRONTEND_EXPECTED_PATHS = {
    "/auth/login",
    "/auth/logout",
    "/auth/register",
    "/bootstrap",
    "/content/posts",
    "/content/threads",
    "/events/{slug}",
    "/feeds/personal",
    "/feeds/public",
    "/governance/comments",
    "/governance/reports",
    "/governance/votes",
    "/healthz",
    "/messages/conversations",
    "/notifications",
    "/platform",
    "/projects/{slug}",
    "/readyz",
    "/search",
    "/users/me",
}


def test_openapi_contains_frontend_expected_paths() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200, response.text
    paths = set(response.json()["paths"].keys())
    missing = sorted(FRONTEND_EXPECTED_PATHS - paths)
    assert not missing, f"Missing frontend API paths: {missing}"


def test_healthz_is_lightweight_liveness_check() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
