from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import captcha


@pytest.fixture
def unique_ip() -> str:
    return f"10.77.{os.getpid() % 250}.{id(object()) % 250}"


def test_register_rejected_when_signup_disabled(
    client: TestClient, unique_ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "signup_enabled", False)
    response = client.post(
        "/auth/register",
        json={"username": "closedsignup", "password": "password-123"},
        headers={"X-Forwarded-For": unique_ip},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Signups are currently closed"


def test_register_requires_captcha_token_when_secret_is_set(
    client: TestClient, unique_ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "test-secret")
    response = client.post(
        "/auth/register",
        json={"username": "needscaptcha", "password": "password-123"},
        headers={"X-Forwarded-For": unique_ip},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Complete the captcha to create an account"


def test_register_rejects_failed_captcha(
    client: TestClient, unique_ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "test-secret")
    monkeypatch.setattr(captcha, "_siteverify", lambda token, secret, remote_ip: False)
    response = client.post(
        "/auth/register",
        json={
            "username": "badcaptcha",
            "password": "password-123",
            "captcha_token": "bad-token",
        },
        headers={"X-Forwarded-For": unique_ip},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Captcha verification failed"


def test_register_accepts_verified_captcha(
    client: TestClient, unique_ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "test-secret")

    def accept_good(token: str, secret: str, remote_ip: str | None) -> bool:
        return token == "good-token"

    monkeypatch.setattr(captcha, "_siteverify", accept_good)
    suffix = str(os.getpid())[-4:]
    response = client.post(
        "/auth/register",
        json={
            "username": f"goodcap{suffix}",
            "password": "password-123",
            "captcha_token": "good-token",
        },
        headers={"X-Forwarded-For": unique_ip},
    )
    assert response.status_code == 200, response.text


def test_siteverify_fails_closed_when_cloudflare_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(captcha.httpx, "post", explode)
    with pytest.raises(Exception) as exc:
        captcha._siteverify("token", "secret", "127.0.0.1")
    assert getattr(exc.value, "status_code", None) == 403
