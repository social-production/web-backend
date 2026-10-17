"""Event plan schedules are stored in UTC.

Authors type dates and times in their Display timezone and send that zone as
``timeZone``. Before saving, the dates and times are rewritten as UTC wall
time and ``timeZone`` becomes ``"UTC"``; each viewer's client converts them
back into the viewer's own zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def _zone(name: object):
    text = str(name or "").strip()
    if not text or text.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(text)
    except Exception:
        return UTC


def _clean_date(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def _clean_time(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        return None
    return text


def _to_utc(day: str, time: str, zone) -> datetime:
    local = datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=zone)
    return local.astimezone(UTC)


def schedule_to_utc(schedule: dict | None, now: datetime | None = None) -> dict:
    """Return a copy of ``schedule`` with its dates and times in UTC."""
    value = dict(schedule or {})
    if not value:
        return value
    zone = _zone(value.get("timeZone"))
    if zone is UTC:
        value["timeZone"] = "UTC"
        return value

    start_date = _clean_date(value.get("startDate"))
    end_date = _clean_date(value.get("endDate")) or start_date
    start_time = _clean_time(value.get("startTimeLabel"))
    finish_time = _clean_time(value.get("finishTimeLabel"))
    # A date with no time is a whole day and stays as typed; times without a
    # date (any day) are converted on today's date.
    reference_day = (now or datetime.now(UTC)).astimezone(zone).date().isoformat()

    if start_time:
        start = _to_utc(start_date or reference_day, start_time, zone)
        value["startTimeLabel"] = start.strftime("%H:%M")
        if start_date:
            value["startDate"] = start.date().isoformat()
            value["startAtUtc"] = start.isoformat()

    if finish_time:
        end = _to_utc(end_date or reference_day, finish_time, zone)
        value["finishTimeLabel"] = end.strftime("%H:%M")
        if end_date:
            value["endDate"] = end.date().isoformat()
            value["endAtUtc"] = end.isoformat()

    value["timeZone"] = "UTC"
    return value
