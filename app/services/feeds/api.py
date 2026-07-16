from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Boolean, DateTime, Integer, String, and_, cast, func, literal, null, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    event_tags,
    event_updates,
    events,
    help_request_tags,
    help_requests,
    posts,
    project_tags,
    project_updates,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    user_follows,
    users,
    user_settings,
)

from app.services.access_control import (
    assert_can_view_scope,
    closed_community_only_tag_condition,
)
from app.services.projects_phases import display_stage_label as project_display_stage_label
from app.services.content import _help_request_role_summaries, _load_help_request_roles

VALID_SORTS = frozenset({"popular", "recent"})

EVENT_STAGE_LABEL_BY_PHASE_ID = {
    "proposal": "Proposal",
    "event-plan": "Event Plan",
    "activity": "Activity",
    "closed": "Closed",
}

_ZERO_INT = literal(0, Integer)
_EMPTY_ROLES = cast(literal("[]"), JSONB)

from app.services.feeds.builder import _build_feed
from app.services.feeds.scope import _get_followed_user_ids, _get_user_scope_ids
from app.services.feeds.selects import (
    _comment_activity_select,
    _comments_select_for_followed,
    _events_select,
    _events_select_for_followed,
    _help_requests_select,
    _help_requests_select_for_followed,
    _posts_select_discovery,
    _posts_select_for_followed,
    _projects_select,
    _projects_select_for_followed,
    _threads_select,
    _threads_select_discovery,
    _threads_select_for_followed,
)
from app.services.feeds.serializers import (
    _fetch_active_votes_for_rows,
    _fetch_latest_updates_for_items,
    _fetch_tags_for_items,
    _serialize_personal_item,
)
from app.services.content import _help_request_role_summaries, _load_help_request_roles

def get_public_feed(
    db: Session,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    return _build_feed(
        db,
        safe_sort,
        max(1, min(limit, 100)),
        max(0, offset),
        current_user_id=current_user_id,
        public_only=True,
    )


def get_home_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    channel_ids, community_ids = _get_user_scope_ids(db, current_user_id)
    return _build_feed(
        db, safe_sort, max(1, min(limit, 100)), max(0, offset),
        channel_ids=channel_ids,
        community_ids=community_ids,
        current_user_id=current_user_id,
    )


def get_personal_feed(
    db: Session,
    current_user_id: UUID,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
    scope: str = "following",
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    normalized_scope = scope.strip().lower()

    followed_user_ids = _get_followed_user_ids(db, current_user_id)
    # Always include the viewer's own posts alongside posts from followed users.
    post_author_ids = list({current_user_id, *followed_user_ids})

    parts = [
        _posts_select_for_followed(post_author_ids),
        _projects_select_for_followed(followed_user_ids),
        _threads_select_for_followed(followed_user_ids),
        _events_select_for_followed(followed_user_ids),
        _help_requests_select_for_followed(followed_user_ids),
        *_comments_select_for_followed(followed_user_ids, current_user_id),
    ]
    if normalized_scope == "popular":
        excluded_author_ids = post_author_ids
        parts.extend(
            [
                _posts_select_discovery(excluded_author_ids),
                _threads_select_discovery(excluded_author_ids),
            ]
        )
    parts = [part for part in parts if part is not None]

    if not parts:
        return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}

    combined = union_all(*parts).subquery("personal_feed")

    if safe_sort == "popular":
        sort_col = (
            combined.c.signal_count
            + combined.c.vote_count
            + combined.c.comment_count
            + combined.c.member_count
            + combined.c.going_count
        ).desc()
    else:
        sort_col = combined.c.last_activity_at.desc()

    rows = db.execute(
        select(combined)
        .order_by(sort_col, combined.c.created_at.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    ).mappings().all()

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, current_user_id)
    help_roles_by_id = _load_help_request_roles(db, help_request_ids, current_user_id)
    items = []
    for row in rows:
        item = _serialize_personal_item(row, tags, active_votes, updates)
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
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_user_feed(
    db: Session,
    username: str,
    viewer_user_id: UUID | None = None,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)

    user_row = db.execute(
        select(users.c.id).where(users.c.username == username.strip().lower())
    ).first()
    if user_row is None:
        return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}

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
        return {
            "total": 0,
            "sort": safe_sort,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "items": [],
        }

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
            literal("activity").label("feed_source"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(
            posts.c.author_id == user_id,
            posts.c.audience.in_(post_audiences),
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
            literal("activity").label("feed_source"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(threads.c.author_id == user_id)
    )

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
            _ZERO_INT.label("signal_count"),
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
            literal("activity").label("feed_source"),
        )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(events.c.created_by == user_id)
    )
    if not viewer_is_owner:
        events_q = events_q.where(events.c.is_private.is_(False))

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
            literal("activity").label("feed_source"),
        )
        .select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))
        .where(projects.c.author_id == user_id, projects.c.is_closed.is_(False))
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
            literal("activity").label("feed_source"),
        )
        .select_from(help_requests.outerjoin(users, users.c.id == help_requests.c.author_id))
        .where(help_requests.c.author_id == user_id)
    )

    combined = union_all(
        posts_q,
        threads_q,
        events_q,
        projects_q,
        help_requests_q,
        *_comments_select_for_followed(
            [user_id],
            viewer_user_id if viewer_user_id is not None else user_id,
            feed_source="activity",
        ),
    ).subquery("user_feed")

    if safe_sort == "popular":
        sort_col = (
            combined.c.signal_count
            + combined.c.vote_count
            + combined.c.comment_count
            + combined.c.member_count
            + combined.c.going_count
        ).desc()
    else:
        sort_col = combined.c.last_activity_at.desc()

    rows = db.execute(
        select(combined)
        .order_by(sort_col, combined.c.created_at.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    ).mappings().all()

    project_ids = [row["id"] for row in rows if row["entity_type"] == "project"]
    thread_ids = [row["id"] for row in rows if row["entity_type"] == "thread"]
    event_ids = [row["id"] for row in rows if row["entity_type"] == "event"]
    help_request_ids = [row["id"] for row in rows if row["entity_type"] == "help_request"]
    tags = _fetch_tags_for_items(db, project_ids, thread_ids, event_ids, help_request_ids)
    updates = _fetch_latest_updates_for_items(db, project_ids, event_ids)
    active_votes = _fetch_active_votes_for_rows(db, rows, viewer_user_id)
    help_roles_by_id = _load_help_request_roles(db, help_request_ids, viewer_user_id)
    items = []
    for row in rows:
        item = _serialize_personal_item(row, tags, active_votes, updates)
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
        "limit": bounded_limit,
        "offset": bounded_offset,
        "items": items,
    }


def get_scope_feed(
    db: Session,
    scope_kind: str,
    slug: str,
    sort: str = "recent",
    limit: int = 20,
    offset: int = 0,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    safe_sort = sort.strip().lower() if sort.strip().lower() in VALID_SORTS else "recent"
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    normalized_slug = slug.strip().lower()

    if scope_kind == "channel":
        row = db.execute(
            select(channels.c.id).where(channels.c.slug == normalized_slug)
        ).first()
        if row is None:
            return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
        return _build_feed(db, safe_sort, bounded_limit, bounded_offset, channel_ids=[row[0]], community_ids=[], current_user_id=current_user_id)

    if scope_kind == "community":
        row = db.execute(
            select(communities.c.id).where(communities.c.slug == normalized_slug)
        ).first()
        if row is None:
            return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
        try:
            assert_can_view_scope(db, current_user_id, "community", row[0])
        except HTTPException:
            return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
        return _build_feed(db, safe_sort, bounded_limit, bounded_offset, channel_ids=[], community_ids=[row[0]], current_user_id=current_user_id)

    return {"total": 0, "sort": safe_sort, "limit": bounded_limit, "offset": bounded_offset, "items": []}
