from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.feeds.location_display import is_placeholder_label
from app.services.feeds.ranking import calendar_window_end, calendar_window_start


def test_calendar_window_end_today_is_exclusive() -> None:
    start = calendar_window_start("today", "UTC")
    end = calendar_window_end("today", "UTC")
    assert start is not None
    assert end is not None
    assert end > start
    assert (end - start).total_seconds() == pytest.approx(86400, rel=0.01)


def test_is_placeholder_label() -> None:
    assert is_placeholder_label("Not specified")
    assert is_placeholder_label("TBD")
    assert not is_placeholder_label("Town Hall")


def test_tomorrow_outside_today_window() -> None:
    tz = "UTC"
    start = calendar_window_start("today", tz)
    end = calendar_window_end("today", tz)
    assert start is not None
    assert end is not None
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    assert not (start <= tomorrow < end)
