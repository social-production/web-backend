from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.cookies import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE
from app.config import Settings
from app.main import app


def _valid_message_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unique_ip():
    """Avoid auth rate-limit collisions across tests."""
    return f"10.{uuid4().int % 250}.{uuid4().int % 250}.{uuid4().int % 250}"


def _register_and_login(client: TestClient, username: str, *, unique_ip: str) -> dict[str, str]:
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    response = client.post(
        "/auth/login", json={"username": username, "password": password}, headers=headers
    )
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(CSRF_COOKIE, "")
    return {
        "access": client.cookies.get(ACCESS_COOKIE, ""),
        "refresh": client.cookies.get(REFRESH_COOKIE, ""),
        "csrf": csrf or "",
        "headers": headers,
    }


def test_login_sets_httponly_cookies(client: TestClient, unique_ip: str):
    username = "sec-cookie-user"
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    response = client.post(
        "/auth/login", json={"username": username, "password": password}, headers=headers
    )
    assert response.status_code == 200

    set_cookie_headers = response.headers.get_list("set-cookie")
    access_header = next(h for h in set_cookie_headers if h.startswith(f"{ACCESS_COOKIE}="))
    assert "httponly" in access_header.lower()
    assert "samesite=lax" in access_header.lower()


def test_login_json_omits_tokens_without_include_header(client: TestClient, unique_ip: str):
    username = "sec-no-tokens"
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    response = client.post(
        "/auth/login", json={"username": username, "password": password}, headers=headers
    )
    body = response.json()
    assert "user" in body
    assert body.get("access_token") is None
    assert body.get("refresh_token") is None


def test_login_json_includes_tokens_with_include_header(client: TestClient, unique_ip: str):
    username = "sec-with-tokens"
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip, "X-Include-Tokens": "true"}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers=headers,
    )
    body = response.json()
    assert body.get("access_token")
    assert body.get("refresh_token")


def test_csrf_rejects_cookie_session_without_header(client: TestClient, unique_ip: str):
    username = "sec-csrf-block"
    session = _register_and_login(client, username, unique_ip=unique_ip)
    assert session["access"]

    response = client.post("/auth/logout", headers=session["headers"])
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_csrf_allows_cookie_session_with_matching_header(client: TestClient, unique_ip: str):
    username = "sec-csrf-ok"
    session = _register_and_login(client, username, unique_ip=unique_ip)

    response = client.post(
        "/auth/logout",
        headers={**session["headers"], "X-CSRF-Token": session["csrf"]},
    )
    assert response.status_code == 200


def test_bearer_auth_bypasses_csrf(client: TestClient, unique_ip: str):
    username = "sec-bearer"
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip, "X-Include-Tokens": "true"}
    client.post(
        "/auth/register", json={"username": username, "password": password}, headers=headers
    )
    login = client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers=headers,
    )
    token = login.json()["access_token"]

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_production_rejects_weak_jwt_secret():
    settings = Settings(
        app_env="production",
        jwt_secret="dev-only-change-me",
        message_encryption_key=_valid_message_key(),
        cors_origins="https://example.com",
    )
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        settings.validate_runtime_settings()


def test_production_rejects_empty_message_key():
    settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key="",
        cors_origins="https://example.com",
    )
    with pytest.raises(RuntimeError, match="MESSAGE_ENCRYPTION_KEY"):
        settings.validate_runtime_settings()


def test_production_rejects_placeholder_message_key():
    settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key="dev-only-change-me-too",
        cors_origins="https://example.com",
    )
    with pytest.raises(RuntimeError, match="MESSAGE_ENCRYPTION_KEY"):
        settings.validate_runtime_settings()


def test_production_rejects_revoked_message_key_digest(monkeypatch):
    revoked_key = _valid_message_key()
    digest = hashlib.sha256(revoked_key.encode("utf-8")).hexdigest()
    monkeypatch.setattr(
        "app.config._REVOKED_MESSAGE_KEY_DIGESTS",
        frozenset({digest}),
    )
    settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key=revoked_key,
        cors_origins="https://example.com",
    )
    with pytest.raises(RuntimeError, match="MESSAGE_ENCRYPTION_KEY"):
        settings.validate_runtime_settings()


def test_production_rejects_invalid_fernet_message_key():
    settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key="not-a-fernet-key",
        cors_origins="https://example.com",
    )
    with pytest.raises(RuntimeError, match="MESSAGE_ENCRYPTION_KEY"):
        settings.validate_runtime_settings()


def test_production_rejects_wildcard_cors():
    settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key=_valid_message_key(),
        cors_origins="*",
    )
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        settings.validate_runtime_settings()


def test_refresh_rotates_tokens(client: TestClient, unique_ip: str):
    username = "sec-refresh"
    session = _register_and_login(client, username, unique_ip=unique_ip)
    old_refresh = session["refresh"]

    refresh_response = client.post("/auth/refresh", headers=session["headers"])
    assert refresh_response.status_code == 200
    new_refresh = client.cookies.get(REFRESH_COOKIE)
    assert new_refresh
    assert new_refresh != old_refresh

    stale = client.post(
        "/auth/refresh", cookies={REFRESH_COOKIE: old_refresh}, headers=session["headers"]
    )
    assert stale.status_code == 401


def test_refresh_token_uses_thirty_day_sliding_lifetime(client: TestClient, unique_ip: str):
    from datetime import UTC, datetime

    from app.auth.jwt import verify_refresh_token
    from app.config import get_settings

    settings = get_settings()
    assert settings.jwt_refresh_expire_days == 30

    username = "sec-refresh-30d"
    session = _register_and_login(client, username, unique_ip=unique_ip)
    before = datetime.now(UTC).timestamp()
    payload = verify_refresh_token(session["refresh"])
    lifetime_seconds = int(payload["exp"]) - before
    expected = 30 * 24 * 60 * 60
    assert expected - 120 <= lifetime_seconds <= expected + 120

    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": "password-123"},
        headers=session["headers"],
    )
    assert login_response.status_code == 200
    refresh_cookie_header = next(
        h
        for h in login_response.headers.get_list("set-cookie")
        if h.startswith(f"{REFRESH_COOKIE}=")
    )
    assert "max-age=2592000" in refresh_cookie_header.lower()

    refresh_response = client.post("/auth/refresh", headers=session["headers"])
    assert refresh_response.status_code == 200
    rotated = client.cookies.get(REFRESH_COOKIE)
    assert rotated
    rotated_payload = verify_refresh_token(rotated)
    rotated_before = datetime.now(UTC).timestamp()
    rotated_lifetime = int(rotated_payload["exp"]) - rotated_before
    assert expected - 120 <= rotated_lifetime <= expected + 120


def test_logout_revokes_current_device_refresh(client: TestClient, unique_ip: str):
    username = "sec-logout-refresh"
    session = _register_and_login(client, username, unique_ip=unique_ip)
    refresh_before = session["refresh"]

    logout_response = client.post(
        "/auth/logout",
        headers={**session["headers"], "X-CSRF-Token": session["csrf"]},
    )
    assert logout_response.status_code == 200

    stale = client.post(
        "/auth/refresh",
        cookies={REFRESH_COOKIE: refresh_before},
        headers=session["headers"],
    )
    assert stale.status_code == 401


def test_expired_refresh_token_is_rejected(client: TestClient, unique_ip: str):
    from datetime import timedelta

    from app.auth.jwt import create_refresh_token

    username = "sec-expired-refresh"
    session = _register_and_login(client, username, unique_ip=unique_ip)
    expired = create_refresh_token("unused-subject", expires_delta=timedelta(seconds=-5))

    response = client.post(
        "/auth/refresh",
        cookies={REFRESH_COOKIE: expired},
        headers=session["headers"],
    )
    assert response.status_code == 401


def test_auth_rate_limit_fail_closed_in_production(monkeypatch):
    import asyncio

    from starlette.requests import Request

    from app.services.auth import enforce_auth_rate_limit

    production_settings = Settings(
        app_env="production",
        jwt_secret="a-very-strong-production-secret-value",
        message_encryption_key=_valid_message_key(),
        cors_origins="https://example.com",
        rate_limit_fail_closed=True,
    )
    monkeypatch.setattr("app.services.auth.get_settings", lambda: production_settings)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(b"x-forwarded-for", b"10.88.0.1")],
        "client": ("127.0.0.1", 1234),
    }
    request = Request(scope)

    async def run() -> None:
        with patch("app.services.auth.get_redis_client") as mock_redis:
            mock_redis.return_value.incr = AsyncMock(side_effect=ConnectionError("redis down"))
            with pytest.raises(HTTPException) as exc_info:
                await enforce_auth_rate_limit(request)
            assert exc_info.value.status_code == 503

    asyncio.run(run())


def test_get_client_ip_uses_forwarded_for():
    from app.utils.request import get_client_ip

    class FakeClient:
        host = "10.0.0.1"

    class FakeRequest:
        headers = {"x-forwarded-for": "203.0.113.50, 10.0.0.1"}
        client = FakeClient()

    assert get_client_ip(FakeRequest()) == "203.0.113.50"
