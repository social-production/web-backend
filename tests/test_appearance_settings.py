"""New accounts start with one Home feed; accent colour and font are saved and checked."""

from __future__ import annotations

from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.models import user_settings
from tests.conftest import seed_user


def _auth(user_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def test_feeds_combined_by_default_and_style_choices_round_trip(db_transaction, isolated_client):
    user_id, _ = seed_user(db_transaction, username_prefix="appearance")
    # A settings row with only database defaults, as a new account gets.
    db_transaction.execute(insert(user_settings).values(user_id=user_id))
    db_transaction.flush()
    headers = _auth(user_id)

    me = isolated_client.get("/users/me", headers=headers)
    assert me.status_code == 200, me.text
    settings = me.json()["settings"]
    assert settings["combine_feeds"] is True
    assert settings["accent_color"] == "green"
    assert settings["font_family"] == "inter"

    saved = isolated_client.patch(
        "/users/me/settings",
        headers=headers,
        json={"accent_color": "Purple", "font_family": "readable", "preferred_language": "ru"},
    )
    assert saved.status_code == 200, saved.text
    after = saved.json()["settings"]
    assert after["accent_color"] == "purple"
    assert after["font_family"] == "readable"
    assert after["preferred_language"] == "ru"

    for bad in ({"accent_color": "neon"}, {"font_family": "comic"}):
        refused = isolated_client.patch("/users/me/settings", headers=headers, json=bad)
        assert refused.status_code == 422, refused.text
