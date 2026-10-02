from __future__ import annotations

import httpx
from fastapi import HTTPException, status

from app.config import get_settings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile_token(token: str | None, remote_ip: str | None) -> None:
    """Reject signup when a Turnstile secret is configured and the token is missing or invalid.

    With no secret, local development keeps working without a captcha.
    """
    secret = get_settings().turnstile_secret_key.strip()
    if not secret:
        return

    provided = (token or "").strip()
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Complete the captcha to create an account",
        )

    if not _siteverify(provided, secret, remote_ip):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Captcha verification failed",
        )


def _siteverify(token: str, secret: str, remote_ip: str | None) -> bool:
    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        response = httpx.post(SITEVERIFY_URL, data=payload, timeout=8.0)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Captcha verification failed",
        ) from exc
    return bool(isinstance(body, dict) and body.get("success"))
