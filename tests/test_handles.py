from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.cookies import CSRF_COOKIE
from app.main import app
from app.utils.handles import canonicalize_handle, validate_handle


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unique_ip():
    return f"10.88.{os.getpid() % 250}.{id(object()) % 250}"


def _auth_headers(client: TestClient, username: str, *, unique_ip: str) -> dict[str, str]:
    password = "password-123"
    headers = {"X-Forwarded-For": unique_ip}
    register = client.post(
        "/auth/register",
        json={"username": username, "password": password},
        headers=headers,
    )
    assert register.status_code == 200, register.text
    csrf = client.cookies.get(CSRF_COOKIE, "")
    return {**headers, "X-CSRF-Token": csrf or ""}


def test_validate_handle_accepts_mixed_case():
    assert validate_handle("Dog-Man") == "Dog-Man"
    assert canonicalize_handle("Dog-Man") == "dog-man"


@pytest.mark.parametrize(
    "value",
    [
        "dog man",
        "ab",
        "a" * 33,
        "-dog",
        "dog-",
        "dog--man",
        "dog__man",
        "dog_-",
        "dog!",
    ],
)
def test_validate_handle_rejects_invalid(value: str):
    with pytest.raises(HTTPException) as exc_info:
        validate_handle(value)
    assert exc_info.value.status_code == 422


def test_register_preserves_display_case_and_blocks_case_collision(
    client: TestClient, unique_ip: str
):
    headers = {"X-Forwarded-For": unique_ip}
    suffix = str(os.getpid())[-4:]
    display = f"Dog-Man{suffix}"
    first = client.post(
        "/auth/register",
        json={"username": display, "password": "password-123"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["user"]["username"] == display

    collision = client.post(
        "/auth/register",
        json={"username": display.lower(), "password": "password-123"},
        headers={"X-Forwarded-For": f"{unique_ip}.2"},
    )
    assert collision.status_code == 409

    login = client.post(
        "/auth/login",
        json={"username": display.upper(), "password": "password-123"},
        headers=headers,
    )
    assert login.status_code == 200
    assert login.json()["user"]["username"] == display

    profile = client.get(f"/users/{display.lower()}")
    assert profile.status_code == 200
    assert profile.json()["user"]["username"] == display


def test_register_rejects_spaces(client: TestClient, unique_ip: str):
    response = client.post(
        "/auth/register",
        json={"username": "dog man", "password": "password-123"},
        headers={"X-Forwarded-For": unique_ip},
    )
    assert response.status_code == 422


def test_channel_handle_policy(client: TestClient, unique_ip: str):
    suffix = str(os.getpid())[-4:]
    headers = _auth_headers(client, f"HandleOwner{suffix}", unique_ip=unique_ip)
    handle_display = f"Dog-Man{suffix}"
    handle_canonical = handle_display.lower()

    bad = client.post(
        "/scopes/channels",
        json={
            "slug": handle_canonical,
            "name": f"dog man{suffix}",
            "description": "topic surface",
        },
        headers=headers,
    )
    assert bad.status_code == 422

    created = client.post(
        "/scopes/channels",
        json={
            "slug": handle_canonical,
            "name": handle_display,
            "description": "topic surface",
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()["channel"]
    assert body["name"] == handle_display
    assert body["slug"] == handle_canonical

    collision = client.post(
        "/scopes/communities",
        json={
            "slug": handle_display.upper(),
            "name": handle_display.upper(),
            "description": "community surface",
            "join_policy": "open",
        },
        headers=headers,
    )
    assert collision.status_code == 409

    fetched = client.get(f"/scopes/channels/{handle_display}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["channel"]["slug"] == handle_canonical
    assert fetched.json()["channel"]["name"] == handle_display
