from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Float, and_, case, cast, func, or_
from sqlalchemy.sql.selectable import Subquery

VALID_SORTS = frozenset({"trending", "recent", "oldest", "top"})
VALID_WINDOWS = frozenset({"today", "week", "month", "all", "custom"})
VALID_FILTERS = frozenset({"all", "projects", "threads", "events", "help_requests"})

_FILTER_TO_ENTITY_TYPE = {
    "projects": "project",
    "threads": "thread",
    "events": "event",
    "help_requests": "help_request",
}

_WINDOW_TIMEDELTAS: dict[str, timedelta] = {
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def resolve_display_timezone(timezone_name: str | None) -> ZoneInfo:
    cleaned = (timezone_name or "").strip()
    if not cleaned:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(cleaned)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def calendar_window_start(window: str, timezone_name: str | None = None) -> datetime | None:
    """Return UTC-aware start instant for preset windows in the viewer timezone."""
    normalized = normalize_window(window)
    if normalized in {"all", "custom"}:
        return None

    tz = resolve_display_timezone(timezone_name)
    now_local = datetime.now(tz)

    if normalized == "today":
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif normalized == "week":
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=now_local.weekday()
        )
    elif normalized == "month":
        start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        delta = _WINDOW_TIMEDELTAS.get(normalized)
        if delta is None:
            return None
        return datetime.now(UTC) - delta

    return start_local.astimezone(UTC)


def calendar_window_end(window: str, timezone_name: str | None = None) -> datetime | None:
    """Return UTC-aware exclusive end instant for preset calendar windows."""
    normalized = normalize_window(window)
    if normalized in {"all", "custom"}:
        return None

    tz = resolve_display_timezone(timezone_name)
    start_local = calendar_window_start(window, timezone_name)
    if start_local is None:
        return None
    start_in_tz = start_local.astimezone(tz)

    if normalized == "today":
        end_local = start_in_tz + timedelta(days=1)
    elif normalized == "week":
        end_local = start_in_tz + timedelta(days=7)
    elif normalized == "month":
        if start_in_tz.month == 12:
            end_local = start_in_tz.replace(year=start_in_tz.year + 1, month=1)
        else:
            end_local = start_in_tz.replace(month=start_in_tz.month + 1)
    else:
        delta = _WINDOW_TIMEDELTAS.get(normalized)
        if delta is None:
            return None
        return datetime.now(UTC)

    return end_local.astimezone(UTC)


def normalize_sort(sort: str | None) -> str:
    normalized = (sort or "").strip().lower()
    if normalized in ("trending", "popular"):
        return "trending"
    if normalized == "recent":
        return "recent"
    if normalized == "oldest":
        return "oldest"
    if normalized == "top":
        return "top"
    return "trending"


def normalize_window(window: str | None) -> str:
    normalized = (window or "").strip().lower()
    return normalized if normalized in VALID_WINDOWS else "all"


def normalize_filter(entity_filter: str | None) -> str:
    normalized = (entity_filter or "").strip().lower()
    return normalized if normalized in VALID_FILTERS else "all"


def filter_entity_type(entity_filter: str) -> str | None:
    """Returns the single entity_type a non-'all' filter restricts to, else None."""
    return _FILTER_TO_ENTITY_TYPE.get(entity_filter)


def window_cutoff(window: str, timezone_name: str | None = None):
    calendar_start = calendar_window_start(window, timezone_name)
    if calendar_start is not None:
        return calendar_start
    delta = _WINDOW_TIMEDELTAS.get(window)
    if delta is None:
        return None
    return datetime.now(UTC) - delta


def apply_window_filter(query, last_activity_col, window: str, timezone_name: str | None = None):
    if window == "custom":
        return query
    cutoff = window_cutoff(window, timezone_name)
    if cutoff is None:
        return query
    return query.where(last_activity_col >= cutoff)


def apply_schedule_window_filter(query, combined, window: str, timezone_name: str | None = None):
    """Filter feed rows by when items happen (scheduled_at), not last activity."""
    normalized = normalize_window(window)
    if normalized in {"all", "custom"}:
        return query

    start = calendar_window_start(normalized, timezone_name)
    end = calendar_window_end(normalized, timezone_name)
    if start is None:
        return query

    scheduled_bounds = [
        combined.c.scheduled_at.is_not(None),
        combined.c.scheduled_at >= start,
    ]
    if end is not None:
        scheduled_bounds.append(combined.c.scheduled_at < end)

    scheduled_clause = and_(
        combined.c.entity_type.in_(("project", "event", "help_request")),
        *scheduled_bounds,
    )

    thread_bounds = [combined.c.last_activity_at >= start]
    if end is not None:
        thread_bounds.append(combined.c.last_activity_at < end)
    thread_clause = and_(combined.c.entity_type == "thread", *thread_bounds)

    return query.where(or_(scheduled_clause, thread_clause))


def trending_score_column(combined: Subquery):
    """Type-aware trending score with caps and recency decay.

    decay = 1 / (1 + hours_since_last_activity / 36)
    projects/events: least(support,50)*2 + least(favorability*20,20) + least(comments,30)
                      + least(members+going,40)
    threads/posts/help: least(greatest(vote_count,0),40) + least(comments,30)
    favorability = support/(support+oppose) when total>0 else 0
    """
    total_signals = combined.c.support_count + combined.c.oppose_count
    favorability = case(
        (total_signals > 0, cast(combined.c.support_count, Float) / cast(total_signals, Float)),
        else_=0.0,
    )
    hours_since_activity = func.extract("epoch", func.now() - combined.c.last_activity_at) / 3600.0
    decay = 1.0 / (1.0 + hours_since_activity / 36.0)

    signal_based_score = (
        func.least(combined.c.support_count, 50) * 2
        + func.least(favorability * 20.0, 20.0)
        + func.least(combined.c.comment_count, 30)
        + func.least(combined.c.member_count + combined.c.going_count, 40)
    )
    vote_based_score = func.least(func.greatest(combined.c.vote_count, 0), 40) + func.least(
        combined.c.comment_count, 30
    )
    base_score = case(
        (combined.c.entity_type.in_(("project", "event")), signal_based_score),
        else_=vote_based_score,
    )
    return (base_score * decay).label("trending_score")


def top_score_column(combined: Subquery):
    """Highest-rated first: raw net score, no recency decay.

    Negatives stay negative so downvoted items sink below zero/unvoted.
    projects/events: support - oppose
    posts/threads/help/comments: vote_count
    """
    signal_net = combined.c.support_count - combined.c.oppose_count
    return case(
        (combined.c.entity_type.in_(("project", "event")), signal_net),
        else_=combined.c.vote_count,
    ).label("top_score")


def sort_order_columns(combined: Subquery, sort: str):
    if sort == "recent":
        return [combined.c.last_activity_at.desc(), combined.c.created_at.desc()]
    if sort == "oldest":
        return [combined.c.last_activity_at.asc(), combined.c.created_at.asc()]
    if sort == "top":
        return [top_score_column(combined).desc(), combined.c.last_activity_at.desc()]
    return [trending_score_column(combined).desc(), combined.c.last_activity_at.desc()]
