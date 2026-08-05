from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    cast,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    event_memberships,
    events,
    help_requests,
    posts,
    projects,
    threads,
    user_follows,
    user_settings,
    users,
)
from app.services.access_control import (
    assert_can_view_scope,
)
from app.services.content import _help_request_role_summaries, _load_help_request_roles
from app.services.feeds.builder import _build_feed
from app.services.feeds.ranking import (
    apply_schedule_window_filter as _apply_schedule_window_filter,
)
from app.services.feeds.ranking import (
    normalize_filter,
    normalize_sort,
    normalize_window,
    sort_order_columns,
)
from app.services.feeds.scope import _get_followed_user_ids, _get_user_scope_ids
from app.services.feeds.selects import (
    _comments_select_for_followed,
    _event_signal_subqueries,
    _events_select_for_followed,
    _events_select_for_member,
    _help_requests_select_for_followed,
    _posts_select_discovery,
    _posts_select_for_followed,
    _project_signal_subqueries,
    _projects_select_for_followed,
    _threads_select_discovery,
    _threads_select_for_followed,
)
from app.services.feeds.serializers import (
    _fetch_active_reports,
    _fetch_active_votes_for_rows,
    _fetch_latest_updates_for_items,
    _fetch_tags_for_items,
    _fetch_viewer_signals_for_rows,
    _serialize_personal_item,
)
from app.utils.usernames import username_matches

EVENT_STAGE_LABEL_BY_PHASE_ID = {
    "proposal": "Proposal",
    "event-plan": "Event Plan",
    "activity": "Activity",
    "closed": "Closed",
}

_ZERO_INT = literal(0, Integer)
_EMPTY_ROLES = cast(literal("[]"), JSONB)

_ENTITY_TYPE_BY_FILTER = {
    "projects": "project",
    "threads": "thread",
    "events": "event",
    "help_requests": "help_request",
}


def get_public_feed(
    db: Session,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
    window: str = "all",
    entity_filter: str = "all",
) -> dict[str, object]:
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    return _build_feed(
        db,
        safe_sort,
        max(1, min(limit, 100)),
        max(0, offset),
        current_user_id=current_user_id,
        public_only=True,
        window=safe_window,
        entity_filter=safe_filter,
    )


def get_home_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    window: str = "all",
    entity_filter: str = "all",
) -> dict[str, object]:
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    channel_ids, community_ids = _get_user_scope_ids(db, current_user_id)
    return _build_feed(
        db,
        safe_sort,
        max(1, min(limit, 100)),
        max(0, offset),
        channel_ids=channel_ids,
        community_ids=community_ids,
        current_user_id=current_user_id,
        window=safe_window,
        entity_filter=safe_filter,
    )


def get_personal_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    scope: str = "following",
    window: str = "all",
    entity_filter: str = "all",
) -> dict[str, object]:
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    normalized_scope = scope.strip().lower()

    followed_user_ids = _get_followed_user_ids(db, current_user_id)
    # Always include the viewer's own posts alongside posts from followed users.
    post_author_ids = list({current_user_id, *followed_user_ids})

    # Invite-only/private-community events the viewer belongs to must remain
    # discoverable in their personal feed even without following the organizer.
    member_private_event_ids = (
        db.execute(
            select(event_memberships.c.event_id)
            .select_from(
                event_memberships.join(events, events.c.id == event_memberships.c.event_id)
            )
            .where(
                event_memberships.c.user_id == current_user_id,
                events.c.is_private.is_(True),
            )
        )
        .scalars()
        .all()
    )

    wanted_type = _ENTITY_TYPE_BY_FILTER.get(safe_filter)

    parts = []
    if wanted_type is None:
        parts.extend(
            [
                _posts_select_for_followed(post_author_ids),
                _projects_select_for_followed(followed_user_ids),
                _threads_select_for_followed(followed_user_ids),
                _events_select_for_followed(followed_user_ids),
                _events_select_for_member(member_private_event_ids),
                _help_requests_select_for_followed(followed_user_ids),
                *_comments_select_for_followed(followed_user_ids, current_user_id),
            ]
        )
        if normalized_scope == "popular":
            excluded_author_ids = post_author_ids
            parts.extend(
                [
                    _posts_select_discovery(excluded_author_ids),
                    _threads_select_discovery(excluded_author_ids),
                ]
            )
    elif wanted_type == "project":
        parts.append(_projects_select_for_followed(followed_user_ids))
    elif wanted_type == "thread":
        parts.append(_threads_select_for_followed(followed_user_ids))
        if normalized_scope == "popular":
            parts.append(_threads_select_discovery(post_author_ids))
    elif wanted_type == "event":
        parts.append(_events_select_for_followed(followed_user_ids))
        parts.append(_events_select_for_member(member_private_event_ids))
    elif wanted_type == "help_request":
        parts.append(_help_requests_select_for_followed(followed_user_ids))

    parts = [part for part in parts if part is not None]

    if not parts:
        return {
            "total": 0,
            "sort": safe_sort,
            "window": safe_window,
            "filter": safe_filter,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "items": [],
        }

    combined = union_all(*parts).subquery("personal_feed")

    stmt = select(combined)
    stmt = _apply_schedule_window_filter(stmt, combined, safe_window)
    stmt = (
        stmt.order_by(*sort_order_columns(combined, safe_sort))
        .limit(bounded_limit)
        .offset(bounded_offset)
    )

    rows = db.execute(stmt).mappings().all()

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    viewer_signals = _fetch_viewer_signals_for_rows(db, rows, current_user_id)
    active_reports = _fetch_active_reports(db, rows, current_user_id)
    help_roles_by_id = _load_help_request_roles(db, help_request_ids, current_user_id)
    items = []
    for row in rows:
        item = _serialize_personal_item(
            row,
            tags,
            active_votes,
            updates,
            viewer_signals,
            active_reports=active_reports,
        )
        if row["entity_type"] == "help_request":
            roles = help_roles_by_id.get(str(row["id"]), [])
            item["roles"] = roles
            signup_count, slots_needed = _help_request_role_summaries(roles)
            item["signup_count"] = signup_count
            item["slots_needed"] = slots_needed
        items.append(item)
    return {
        "total": len(items),
        "sort": safe_sort,
        "window": safe_window,
        "filter": safe_filter,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_user_feed(
    db: Session,
    username: str,
    viewer_user_id: UUID | None = None,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    window: str = "all",
    entity_filter: str = "all",
) -> dict[str, object]:
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)

    empty_response = {
        "total": 0,
        "sort": safe_sort,
        "window": safe_window,
        "filter": safe_filter,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": [],
    }

    user_row = db.execute(select(users.c.id).where(username_matches(username))).first()
    if user_row is None:
        return empty_response

    user_id: UUID = user_row[0]
    viewer_is_owner = viewer_user_id is not None and viewer_user_id == user_id
    viewer_is_following = False
    if viewer_user_id is not None and not viewer_is_owner:
        viewer_is_following = (
            db.execute(
                select(user_follows.c.follower_id).where(
                    user_follows.c.follower_id == viewer_user_id,
                    user_follows.c.followed_id == user_id,
                    user_follows.c.status == "accepted",
                )
            ).first()
            is not None
        )

    settings_row = db.execute(
        select(
            user_settings.c.hide_public_profile_activity_from_non_followers,
            user_settings.c.hide_personal_feed_from_non_followers,
        ).where(user_settings.c.user_id == user_id)
    ).first()
    hide_public_profile = bool(settings_row[0]) if settings_row else False
    hide_personal_feed = bool(settings_row[1]) if settings_row else False
    if hide_public_profile and not viewer_is_owner and not viewer_is_following:
        return empty_response

    if viewer_is_owner or viewer_is_following or not hide_personal_feed:
        post_audiences = ["public", "followers"]
    else:
        post_audiences = ["public"]

    posts_q = (
        select(
            posts.c.id,
            literal("post").label("entity_type"),
            literal(None).label("slug"),
            literal("Post").label("title"),
            posts.c.body,
            posts.c.audience,
            posts.c.author_id,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            _ZERO_INT.label("signal_count"),
            _ZERO_INT.label("support_count"),
            _ZERO_INT.label("oppose_count"),
            posts.c.vote_count,
            posts.c.comment_count,
            _ZERO_INT.label("member_count"),
            _ZERO_INT.label("going_count"),
            posts.c.updated_at.label("last_activity_at"),
            posts.c.created_at,
            literal(None).label("project_mode"),
            literal(None).label("project_subtype"),
            literal(None).label("stage_label"),
            literal(None).label("current_phase_id"),
            literal(None).label("location_label"),
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
            posts.c.moderation_state,
            posts.c.moderation_reason,
            literal("activity").label("feed_source"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(
            posts.c.author_id == user_id,
            posts.c.audience.in_(post_audiences),
            posts.c.moderation_state != "removed",
        )
    )

    threads_q = (
        select(
            threads.c.id,
            literal("thread").label("entity_type"),
            threads.c.slug,
            threads.c.title,
            threads.c.body,
            literal(None).label("audience"),
            threads.c.author_id,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            _ZERO_INT.label("signal_count"),
            _ZERO_INT.label("support_count"),
            _ZERO_INT.label("oppose_count"),
            threads.c.vote_count,
            threads.c.comment_count,
            _ZERO_INT.label("member_count"),
            _ZERO_INT.label("going_count"),
            threads.c.last_activity_at,
            threads.c.created_at,
            literal(None).label("project_mode"),
            literal(None).label("project_subtype"),
            literal(None).label("stage_label"),
            literal(None).label("current_phase_id"),
            literal(None).label("location_label"),
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
            threads.c.moderation_state,
            threads.c.moderation_reason,
            literal("activity").label("feed_source"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(threads.c.author_id == user_id, threads.c.moderation_state != "removed")
    )

    total_support, total_oppose = _event_signal_subqueries()
    support_count, oppose_count = _event_signal_subqueries()
    events_q = (
        select(
            events.c.id,
            literal("event").label("entity_type"),
            events.c.slug,
            events.c.title,
            events.c.description.label("body"),
            literal(None).label("audience"),
            events.c.created_by.label("author_id"),
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            (total_support + total_oppose).label("signal_count"),
            support_count.label("support_count"),
            oppose_count.label("oppose_count"),
            events.c.vote_count,
            events.c.comment_count,
            events.c.member_count,
            events.c.going_count,
            events.c.last_activity_at,
            events.c.created_at,
            literal(None).label("project_mode"),
            literal(None).label("project_subtype"),
            literal(None).label("stage_label"),
            events.c.current_phase_id,
            events.c.location_label,
            events.c.is_private,
            events.c.scheduled_at,
            events.c.time_label,
            events.c.moderation_state,
            events.c.moderation_reason,
            literal("activity").label("feed_source"),
        )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(events.c.created_by == user_id, events.c.moderation_state != "removed")
    )
    if not viewer_is_owner:
        events_q = events_q.where(events.c.is_private.is_(False))

    project_support_count, project_oppose_count = _project_signal_subqueries()
    projects_q = (
        select(
            projects.c.id,
            literal("project").label("entity_type"),
            projects.c.slug,
            projects.c.title,
            projects.c.description.label("body"),
            literal(None).label("audience"),
            projects.c.author_id,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            projects.c.signal_count,
            project_support_count.label("support_count"),
            project_oppose_count.label("oppose_count"),
            projects.c.vote_count,
            projects.c.comment_count,
            projects.c.member_count,
            _ZERO_INT.label("going_count"),
            projects.c.last_activity_at,
            projects.c.created_at,
            projects.c.project_mode,
            projects.c.project_subtype,
            projects.c.stage_label,
            projects.c.current_phase_id,
            projects.c.location_label,
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
            projects.c.moderation_state,
            projects.c.moderation_reason,
            literal("activity").label("feed_source"),
        )
        .select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))
        .where(
            projects.c.author_id == user_id,
            projects.c.is_closed.is_(False),
            projects.c.moderation_state != "removed",
        )
    )

    help_requests_q = (
        select(
            help_requests.c.id,
            literal("help_request").label("entity_type"),
            literal(None).label("slug"),
            help_requests.c.title,
            help_requests.c.body,
            literal(None).label("audience"),
            help_requests.c.author_id,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            _ZERO_INT.label("signal_count"),
            _ZERO_INT.label("support_count"),
            _ZERO_INT.label("oppose_count"),
            help_requests.c.vote_count,
            help_requests.c.comment_count,
            _ZERO_INT.label("member_count"),
            _ZERO_INT.label("going_count"),
            help_requests.c.created_at.label("last_activity_at"),
            help_requests.c.created_at,
            literal(None).label("project_mode"),
            literal(None).label("project_subtype"),
            literal(None).label("stage_label"),
            literal(None).label("current_phase_id"),
            help_requests.c.location_label,
            literal(False, Boolean).label("is_private"),
            help_requests.c.needed_at.label("scheduled_at"),
            help_requests.c.schedule_label.label("time_label"),
            help_requests.c.moderation_state,
            help_requests.c.moderation_reason,
            literal("activity").label("feed_source"),
        )
        .select_from(help_requests.outerjoin(users, users.c.id == help_requests.c.author_id))
        .where(help_requests.c.author_id == user_id, help_requests.c.moderation_state != "removed")
    )

    comment_parts = _comments_select_for_followed(
        [user_id],
        viewer_user_id if viewer_user_id is not None else user_id,
        feed_source="activity",
    )

    wanted_type = _ENTITY_TYPE_BY_FILTER.get(safe_filter)
    selects_by_type = {
        "post": [posts_q],
        "thread": [threads_q],
        "event": [events_q],
        "project": [projects_q],
        "help_request": [help_requests_q],
    }
    if wanted_type is None:
        parts = [q for parts in selects_by_type.values() for q in parts] + comment_parts
    else:
        parts = selects_by_type.get(wanted_type, [])

    if not parts:
        return empty_response

    combined = union_all(*parts).subquery("user_feed")

    stmt = select(combined)
    stmt = _apply_schedule_window_filter(stmt, combined, safe_window)
    stmt = (
        stmt.order_by(*sort_order_columns(combined, safe_sort))
        .limit(bounded_limit)
        .offset(bounded_offset)
    )

    rows = db.execute(stmt).mappings().all()

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, viewer_user_id)
    viewer_signals = _fetch_viewer_signals_for_rows(db, rows, viewer_user_id)
    active_reports = _fetch_active_reports(db, rows, viewer_user_id)
    help_roles_by_id = _load_help_request_roles(db, help_request_ids, viewer_user_id)
    items = []
    for row in rows:
        item = _serialize_personal_item(
            row,
            tags,
            active_votes,
            updates,
            viewer_signals,
            active_reports=active_reports,
        )
        if row["entity_type"] == "help_request":
            roles = help_roles_by_id.get(str(row["id"]), [])
            item["roles"] = roles
            signup_count, slots_needed = _help_request_role_summaries(roles)
            item["signup_count"] = signup_count
            item["slots_needed"] = slots_needed
        items.append(item)
    return {
        "total": len(items),
        "sort": safe_sort,
        "window": safe_window,
        "filter": safe_filter,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_scope_feed(
    db: Session,
    scope_kind: str,
    slug: str,
    sort: str = "trending",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
    window: str = "all",
    entity_filter: str = "all",
) -> dict[str, object]:
    safe_sort = normalize_sort(sort)
    safe_window = normalize_window(window)
    safe_filter = normalize_filter(entity_filter)
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    normalized_slug = slug.strip().lower()

    empty_response = {
        "total": 0,
        "sort": safe_sort,
        "window": safe_window,
        "filter": safe_filter,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": [],
    }

    if scope_kind == "channel":
        row = db.execute(select(channels.c.id).where(channels.c.slug == normalized_slug)).first()
        if row is None:
            return empty_response
        return _build_feed(
            db,
            safe_sort,
            bounded_limit,
            bounded_offset,
            channel_ids=[row[0]],
            community_ids=[],
            current_user_id=current_user_id,
            window=safe_window,
            entity_filter=safe_filter,
        )

    if scope_kind == "community":
        row = db.execute(
            select(communities.c.id).where(communities.c.slug == normalized_slug)
        ).first()
        if row is None:
            return empty_response
        try:
            assert_can_view_scope(db, current_user_id, "community", row[0])
        except HTTPException:
            return empty_response
        return _build_feed(
            db,
            safe_sort,
            bounded_limit,
            bounded_offset,
            channel_ids=[],
            community_ids=[row[0]],
            current_user_id=current_user_id,
            window=safe_window,
            entity_filter=safe_filter,
        )

    return empty_response
