"""Direct and group message attachments and pins."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import blobs, message_attachments
from tests.conftest import seed_user

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _seed_pair(prefix: str) -> tuple[tuple, tuple, tuple]:
    db = SessionLocal()
    try:
        owner = seed_user(db, username_prefix=f"{prefix}-a")
        member = seed_user(db, username_prefix=f"{prefix}-b")
        outsider = seed_user(db, username_prefix=f"{prefix}-c")
        db.commit()
        return owner, member, outsider
    finally:
        db.close()


def _direct(client: TestClient, owner, member_name: str) -> str:
    response = client.post(
        "/messages/direct",
        headers=_auth(owner[0]),
        json={"other_username": member_name},
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation"]["id"]


def test_photo_round_trip_is_member_only_and_photo_only_is_allowed(client: TestClient) -> None:
    owner, member, outsider = _seed_pair("photo")
    conversation_id = _direct(client, owner, member[1])

    sent = client.post(
        f"/messages/conversations/{conversation_id}/messages",
        headers=_auth(owner[0]),
        data={"body": ""},
        files={"file": ("dot.png", TINY_PNG, "image/png")},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["message"]
    assert message["body"] == ""
    assert message["attachments"][0]["kind"] == "image"
    attachment_id = message["attachments"][0]["id"]

    downloaded = client.get(f"/messages/attachments/{attachment_id}", headers=_auth(member[0]))
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == TINY_PNG
    assert downloaded.headers["content-type"].startswith("image/png")

    hidden = client.get(f"/messages/attachments/{attachment_id}", headers=_auth(outsider[0]))
    assert hidden.status_code == 404, hidden.text

    db = SessionLocal()
    try:
        stored = db.execute(
            select(blobs.c.ciphertext)
            .select_from(
                blobs.join(
                    message_attachments,
                    blobs.c.storage_key == message_attachments.c.storage_key,
                )
            )
            .where(message_attachments.c.id == attachment_id)
        ).scalar_one()
    finally:
        db.close()
    assert bytes(stored) != TINY_PNG

    listed = client.get("/messages/conversations", headers=_auth(owner[0]))
    assert listed.status_code == 200, listed.text
    preview = next(
        item["preview"] for item in listed.json()["items"] if item["id"] == conversation_id
    )
    assert preview == "Photo"


def test_oversized_file_video_and_oversized_photo_are_rejected(client: TestClient) -> None:
    owner, member, _outsider = _seed_pair("limit")
    conversation_id = _direct(client, owner, member[1])
    url = f"/messages/conversations/{conversation_id}/messages"

    too_big = client.post(
        url,
        headers=_auth(owner[0]),
        data={"body": "notes"},
        files={"file": ("notes.bin", b"a" * (11 * 1024 * 1024), "application/octet-stream")},
    )
    assert too_big.status_code == 422, too_big.text
    assert "10MB" in too_big.json()["detail"]

    video = client.post(
        url,
        headers=_auth(owner[0]),
        data={"body": ""},
        files={"file": ("clip.mp4", b"not-a-video", "video/mp4")},
    )
    assert video.status_code == 422, video.text
    assert "video" in video.json()["detail"].lower()

    huge_photo = client.post(
        url,
        headers=_auth(owner[0]),
        data={"body": ""},
        files={"file": ("big.jpg", b"\xff\xd8\xff" + (b"a" * (1024 * 1024)), "image/jpeg")},
    )
    assert huge_photo.status_code == 422, huge_photo.text
    assert "1MB" in huge_photo.json()["detail"]


def test_pin_cap_and_group_creator_rule(client: TestClient) -> None:
    owner, member, _outsider = _seed_pair("pins")
    conversation_id = _direct(client, owner, member[1])
    message_ids = []
    for index in range(6):
        sent = client.post(
            f"/messages/conversations/{conversation_id}/messages",
            headers=_auth(owner[0]),
            json={"body": f"note {index}"},
        )
        assert sent.status_code == 200, sent.text
        message_ids.append(sent.json()["message"]["id"])

    for message_id in message_ids[:5]:
        pinned = client.post(
            f"/messages/conversations/{conversation_id}/pins/{message_id}",
            headers=_auth(member[0]),
        )
        assert pinned.status_code == 200, pinned.text

    sixth = client.post(
        f"/messages/conversations/{conversation_id}/pins/{message_ids[5]}",
        headers=_auth(member[0]),
    )
    assert sixth.status_code == 422, sixth.text

    group = client.post(
        "/messages/group",
        headers=_auth(owner[0]),
        json={"title": "Pin group", "participant_usernames": [member[1]]},
    )
    assert group.status_code == 200, group.text
    group_id = group.json()["conversation"]["id"]
    sent = client.post(
        f"/messages/conversations/{group_id}/messages",
        headers=_auth(owner[0]),
        json={"body": "group note"},
    )
    assert sent.status_code == 200, sent.text
    group_message_id = sent.json()["message"]["id"]

    denied = client.post(
        f"/messages/conversations/{group_id}/pins/{group_message_id}",
        headers=_auth(member[0]),
    )
    assert denied.status_code == 403, denied.text

    allowed = client.post(
        f"/messages/conversations/{group_id}/pins/{group_message_id}",
        headers=_auth(owner[0]),
    )
    assert allowed.status_code == 200, allowed.text

    listed = client.get(
        f"/messages/conversations/{group_id}/messages",
        headers=_auth(member[0]),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["can_pin"] is False
    assert listed.json()["pins"][0]["message_id"] == group_message_id
    assert listed.json()["pins"][0]["preview"] == "group note"
