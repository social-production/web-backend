from datetime import UTC, datetime

from app.utils.plan_schedule import schedule_to_utc


def test_melbourne_evening_is_stored_as_utc_morning():
    stored = schedule_to_utc(
        {
            "mode": "date",
            "startDate": "2026-10-20",
            "startTimeLabel": "18:00",
            "finishTimeLabel": "21:00",
            "timeZone": "Australia/Melbourne",
        }
    )
    assert stored["timeZone"] == "UTC"
    assert stored["startDate"] == "2026-10-20"
    assert stored["startTimeLabel"] == "07:00"
    assert stored["endDate"] == "2026-10-20"
    assert stored["finishTimeLabel"] == "10:00"
    assert stored["startAtUtc"] == "2026-10-20T07:00:00+00:00"


def test_morning_moves_to_previous_utc_day():
    stored = schedule_to_utc(
        {
            "mode": "range",
            "startDate": "2026-10-20",
            "endDate": "2026-10-21",
            "startTimeLabel": "09:00",
            "finishTimeLabel": "17:00",
            "timeZone": "Australia/Melbourne",
        }
    )
    assert (stored["startDate"], stored["startTimeLabel"]) == ("2026-10-19", "22:00")
    assert (stored["endDate"], stored["finishTimeLabel"]) == ("2026-10-21", "06:00")


def test_any_day_times_and_utc_input_are_kept_simple():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    stored = schedule_to_utc(
        {"mode": "any-day", "startTimeLabel": "18:00", "timeZone": "Australia/Brisbane"}, now
    )
    assert stored["startTimeLabel"] == "08:00"
    assert "startDate" not in stored
    assert schedule_to_utc({"mode": "date", "startDate": "2026-10-20"}) == {
        "mode": "date",
        "startDate": "2026-10-20",
        "timeZone": "UTC",
    }


def test_private_event_plan_is_saved_in_utc(db_session, client):
    from app.auth.jwt import create_access_token
    from tests.conftest import private_event_plan_fields, seed_channel_with_membership, seed_user

    user_id, _ = seed_user(db_session, username_prefix="utc-plan")
    _, channel_slug = seed_channel_with_membership(db_session, creator_id=user_id)
    db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(user_id))}"}
    fields = private_event_plan_fields()
    fields["schedule_payload"] = {
        "mode": "date",
        "startDate": "2030-10-20",
        "startTimeLabel": "18:00",
        "finishTimeLabel": "21:00",
        "timeZone": "Australia/Melbourne",
    }
    created = client.post(
        "/events",
        headers=headers,
        json={
            "title": "Melbourne dinner",
            "description": "Typed in Melbourne time",
            "governance": "organizer_controlled",
            "audience": "invite_only",
            "channel_slugs": [channel_slug],
            "community_slugs": [],
            "invited_usernames": [],
            "time_label": "TBD",
            "location_label": "TBD",
            **fields,
        },
    )
    assert created.status_code == 200, created.text
    detail = client.get(f"/events/{created.json()['event']['slug']}", headers=headers).json()
    schedule = detail["lifecycle"]["phaseTwo"]["plans"][0]["schedule"]
    assert schedule["timeZone"] == "UTC"
    assert (schedule["startDate"], schedule["startTimeLabel"]) == ("2030-10-20", "07:00")
    assert schedule["finishTimeLabel"] == "10:00"
