from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.feeds.ranking import calendar_window_start, normalize_window


def test_calendar_window_start_today_uses_local_midnight() -> None:
    tz = ZoneInfo("Australia/Sydney")
    now_local = datetime.now(tz)
    start = calendar_window_start("today", "Australia/Sydney")
    assert start is not None
    expected = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    assert abs((start - expected).total_seconds()) < 2


def test_calendar_window_start_week_starts_monday() -> None:
    start = calendar_window_start("week", "UTC")
    assert start is not None
    now = datetime.now(UTC)
    expected = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=now.weekday()
    )
    assert abs((start - expected).total_seconds()) < 2


def test_normalize_window_custom() -> None:
    assert normalize_window("custom") == "custom"
