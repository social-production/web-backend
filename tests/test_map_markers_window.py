"""Map marker schedule window filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.feeds.ranking import calendar_window_end, calendar_window_start


def _within_preset_window(ts: datetime, *, window: str, timezone_name: str | None) -> bool:
    """Mirror get_map_markers preset-window logic for unit tests."""
    window_start = calendar_window_start(window, timezone_name)
    if window_start is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if ts < window_start:
        return False
    window_end = calendar_window_end(window, timezone_name)
    if window_end is not None and ts >= window_end:
        return False
    return True


def test_tomorrow_excluded_from_today_window() -> None:
    tz = "Australia/Melbourne"
    start = calendar_window_start("today", tz)
    end = calendar_window_end("today", tz)
    assert start is not None
    assert end is not None

    later_today = start + timedelta(hours=2)
    tomorrow = end + timedelta(hours=1)

    assert _within_preset_window(later_today, window="today", timezone_name=tz)
    assert not _within_preset_window(tomorrow, window="today", timezone_name=tz)


def test_today_window_bounds_are_half_open() -> None:
    tz = "UTC"
    start = calendar_window_start("today", tz)
    end = calendar_window_end("today", tz)
    assert start is not None
    assert end is not None
    assert _within_preset_window(start, window="today", timezone_name=tz)
    assert not _within_preset_window(end, window="today", timezone_name=tz)
