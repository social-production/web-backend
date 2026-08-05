from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    and_,
    cast,
    func,
    literal,
    null,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models import (
    comments,
    event_signals,
    event_tags,
    events,
    help_request_tags,
    help_requests,
    posts,
    project_activities,
    project_signals,
    project_tags,
    projects,
    thread_tags,
    threads,
    users,
)
from app.services.access_control import (
    closed_community_only_tag_condition,
)

VALID_SORTS = frozenset({"trending", "recent", "popular", "oldest", "top"})

EVENT_STAGE_LABEL_BY_PHASE_ID = {
    "proposal": "Proposal",
    "event-plan": "Event Plan",
    "activity": "Activity",
    "closed": "Closed",
}

_ZERO_INT = literal(0, Integer)
_EMPTY_ROLES = cast(literal("[]"), JSONB)


def _project_signal_subqueries():
    """Returns (support_count_subquery, oppose_count_subquery) correlated to `projects.c.id`."""
    support = (
        select(func.count())
        .select_from(project_signals)
        .where(
            project_signals.c.project_id == projects.c.id,
            project_signals.c.signal_type == "demand",
        )
        .correlate(projects)
        .scalar_subquery()
    )
    oppose = (
        select(func.count())
        .select_from(project_signals)
        .where(
            project_signals.c.project_id == projects.c.id,
            project_signals.c.signal_type == "opposition",
        )
        .correlate(projects)
        .scalar_subquery()
    )
    return support, oppose


def _event_signal_subqueries():
    """Returns (support_count_subquery, oppose_count_subquery) correlated to `events.c.id`."""
    support = (
        select(func.count())
        .select_from(event_signals)
        .where(
            event_signals.c.event_id == events.c.id,
            event_signals.c.signal_type == "demand",
        )
        .correlate(events)
        .scalar_subquery()
    )
    oppose = (
        select(func.count())
        .select_from(event_signals)
        .where(
            event_signals.c.event_id == events.c.id,
            event_signals.c.signal_type == "opposition",
        )
        .correlate(events)
        .scalar_subquery()
    )
    return support, oppose


def _next_project_activity_scheduled_at():
    return (
        select(func.min(project_activities.c.scheduled_at))
        .where(
            project_activities.c.project_id == projects.c.id,
            project_activities.c.scheduled_at.is_not(None),
        )
        .correlate(projects)
        .scalar_subquery()
    )


def _projects_select(
    channel_ids: list[UUID] | None,
    community_ids: list[UUID] | None,
    *,
    public_only: bool = False,
):
    support_count, oppose_count = _project_signal_subqueries()
    q = select(
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
        support_count.label("support_count"),
        oppose_count.label("oppose_count"),
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
        _next_project_activity_scheduled_at().label("scheduled_at"),
        literal(None).label("time_label"),
        _EMPTY_ROLES.label("roles"),
        projects.c.moderation_state,
        projects.c.moderation_reason,
    ).where(projects.c.is_closed.is_(False), projects.c.moderation_state != "removed")
    q = q.select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(project_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(project_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(project_tags, project_tags.c.project_id == projects.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    if public_only:
        q = q.where(~closed_community_only_tag_condition(project_tags, projects.c.id, "project_id"))
    return q


def _threads_select(
    channel_ids: list[UUID] | None,
    community_ids: list[UUID] | None,
    *,
    public_only: bool = False,
):
    q = select(
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
        _EMPTY_ROLES.label("roles"),
        threads.c.moderation_state,
        threads.c.moderation_reason,
    ).where(threads.c.moderation_state != "removed")
    q = q.select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(thread_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(thread_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(thread_tags, thread_tags.c.thread_id == threads.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    if public_only:
        q = q.where(~closed_community_only_tag_condition(thread_tags, threads.c.id, "thread_id"))
    return q


def _events_select(
    channel_ids: list[UUID] | None,
    community_ids: list[UUID] | None,
    *,
    public_only: bool = False,
):
    total_support, total_oppose = _event_signal_subqueries()
    support_count, oppose_count = _event_signal_subqueries()
    q = select(
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
        _EMPTY_ROLES.label("roles"),
        events.c.moderation_state,
        events.c.moderation_reason,
    ).where(
        events.c.moderation_state != "removed",
        or_(
            events.c.is_private.is_(False),
            and_(
                events.c.audience == "private_community",
                events.c.home_community_id.in_(community_ids or []),
            ),
        ),
    )
    q = q.select_from(events.outerjoin(users, users.c.id == events.c.created_by))

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(event_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(event_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(event_tags, event_tags.c.event_id == events.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    if public_only:
        q = q.where(~closed_community_only_tag_condition(event_tags, events.c.id, "event_id"))
    return q


def _help_requests_select(
    channel_ids: list[UUID] | None,
    community_ids: list[UUID] | None,
    *,
    public_only: bool = False,
):
    q = (
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
            help_requests.c.roles,
            help_requests.c.moderation_state,
            help_requests.c.moderation_reason,
        )
        .where(help_requests.c.moderation_state != "removed")
        .select_from(help_requests.outerjoin(users, users.c.id == help_requests.c.author_id))
    )

    if channel_ids is not None:
        tag_conditions = []
        if channel_ids:
            tag_conditions.append(help_request_tags.c.channel_id.in_(channel_ids))
        if community_ids:
            tag_conditions.append(help_request_tags.c.community_id.in_(community_ids))
        if not tag_conditions:
            return None
        q = (
            q.join(help_request_tags, help_request_tags.c.help_request_id == help_requests.c.id)
            .where(or_(*tag_conditions))
            .distinct()
        )
    if public_only:
        q = q.where(
            ~closed_community_only_tag_condition(
                help_request_tags, help_requests.c.id, "help_request_id"
            )
        )
    return q


def _posts_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
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
            literal("following").label("feed_source"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(posts.c.author_id.in_(followed_user_ids), posts.c.moderation_state != "removed")
    )


def _posts_select_discovery(excluded_author_ids: list[UUID]):
    if not excluded_author_ids:
        return None
    return (
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
            literal("discovery").label("feed_source"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(
            posts.c.audience == "public",
            posts.c.author_id.not_in(excluded_author_ids),
            posts.c.moderation_state != "removed",
        )
    )


def _projects_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    support_count, oppose_count = _project_signal_subqueries()
    return (
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
            support_count.label("support_count"),
            oppose_count.label("oppose_count"),
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
            literal("following").label("feed_source"),
        )
        .select_from(projects.outerjoin(users, users.c.id == projects.c.author_id))
        .where(
            projects.c.author_id.in_(followed_user_ids),
            projects.c.is_closed.is_(False),
            projects.c.moderation_state != "removed",
        )
    )


def _threads_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
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
            literal("following").label("feed_source"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(
            threads.c.author_id.in_(followed_user_ids),
            threads.c.moderation_state != "removed",
        )
    )


def _threads_select_discovery(excluded_author_ids: list[UUID]):
    if not excluded_author_ids:
        return None
    return (
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
            literal("discovery").label("feed_source"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(
            threads.c.author_id.not_in(excluded_author_ids),
            threads.c.moderation_state != "removed",
        )
    )


def _comment_activity_select(
    subject_type: str,
    slug_column,
    title_column,
    subject_id_column,
    join_clause,
    extra_where,
    followed_user_ids: list[UUID],
    feed_source: str = "following",
):
    if not followed_user_ids:
        return None

    comment_replies = comments.alias("comment_replies")
    reply_count_subq = (
        select(func.count())
        .select_from(comment_replies)
        .where(comment_replies.c.parent_id == comments.c.id)
        .correlate(comments)
        .scalar_subquery()
    )

    return (
        select(
            comments.c.id,
            literal("comment_activity").label("entity_type"),
            slug_column.label("slug"),
            title_column.label("title"),
            comments.c.body,
            literal(subject_type).label("audience"),
            comments.c.author_id,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
            _ZERO_INT.label("signal_count"),
            _ZERO_INT.label("support_count"),
            _ZERO_INT.label("oppose_count"),
            comments.c.vote_count.label("vote_count"),
            reply_count_subq.label("comment_count"),
            _ZERO_INT.label("member_count"),
            _ZERO_INT.label("going_count"),
            comments.c.created_at.label("last_activity_at"),
            comments.c.created_at,
            literal(subject_type).label("project_mode"),
            cast(subject_id_column, String).label("project_subtype"),
            literal(None).label("stage_label"),
            literal(None).label("current_phase_id"),
            literal(None).label("location_label"),
            literal(False, Boolean).label("is_private"),
            cast(null(), DateTime(timezone=True)).label("scheduled_at"),
            literal(None).label("time_label"),
            comments.c.moderation_state,
            comments.c.moderation_reason,
            literal(feed_source).label("feed_source"),
        )
        .select_from(join_clause)
        .where(
            comments.c.subject_type == subject_type,
            comments.c.author_id.in_(followed_user_ids),
            comments.c.moderation_state != "removed",
            extra_where,
        )
    )


def _comments_select_for_followed(
    followed_user_ids: list[UUID],
    current_user_id: UUID,
    feed_source: str = "following",
) -> list:
    if not followed_user_ids:
        return []

    visible_post_author_ids = list({current_user_id, *followed_user_ids})
    return [
        part
        for part in [
            _comment_activity_select(
                "thread",
                threads.c.slug,
                threads.c.title,
                comments.c.subject_id,
                comments.outerjoin(users, users.c.id == comments.c.author_id).join(
                    threads, comments.c.subject_id == threads.c.id
                ),
                threads.c.moderation_state != "removed",
                followed_user_ids,
                feed_source=feed_source,
            ),
            _comment_activity_select(
                "post",
                cast(null(), String),
                func.left(posts.c.body, 120),
                comments.c.subject_id,
                comments.outerjoin(users, users.c.id == comments.c.author_id).join(
                    posts, comments.c.subject_id == posts.c.id
                ),
                and_(
                    or_(
                        posts.c.audience == "public",
                        posts.c.author_id.in_(visible_post_author_ids),
                    ),
                    posts.c.moderation_state != "removed",
                ),
                followed_user_ids,
                feed_source=feed_source,
            ),
            _comment_activity_select(
                "project",
                projects.c.slug,
                projects.c.title,
                comments.c.subject_id,
                comments.outerjoin(users, users.c.id == comments.c.author_id).join(
                    projects, comments.c.subject_id == projects.c.id
                ),
                and_(
                    projects.c.is_closed.is_(False),
                    projects.c.moderation_state != "removed",
                ),
                followed_user_ids,
                feed_source=feed_source,
            ),
            _comment_activity_select(
                "event",
                events.c.slug,
                events.c.title,
                comments.c.subject_id,
                comments.outerjoin(users, users.c.id == comments.c.author_id).join(
                    events, comments.c.subject_id == events.c.id
                ),
                and_(
                    events.c.is_private.is_(False),
                    events.c.moderation_state != "removed",
                ),
                followed_user_ids,
                feed_source=feed_source,
            ),
            _comment_activity_select(
                "help_request",
                cast(null(), String),
                help_requests.c.title,
                comments.c.subject_id,
                comments.outerjoin(users, users.c.id == comments.c.author_id).join(
                    help_requests, comments.c.subject_id == help_requests.c.id
                ),
                help_requests.c.moderation_state != "removed",
                followed_user_ids,
                feed_source=feed_source,
            ),
        ]
        if part is not None
    ]


def _events_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    total_support, total_oppose = _event_signal_subqueries()
    support_count, oppose_count = _event_signal_subqueries()
    return (
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
            literal("following").label("feed_source"),
        )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(
            events.c.created_by.in_(followed_user_ids),
            events.c.is_private.is_(False),
            events.c.moderation_state != "removed",
        )
    )


def _events_select_for_member(member_event_ids: list[UUID]):
    """Private-audience events (invite-only or private-community) the viewer belongs to.

    Used by the personal feed so invited/authorized members can discover events
    even when they don't follow the organizer.
    """
    if not member_event_ids:
        return None
    total_support, total_oppose = _event_signal_subqueries()
    support_count, oppose_count = _event_signal_subqueries()
    return (
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
            literal("member").label("feed_source"),
        )
        .select_from(events.outerjoin(users, users.c.id == events.c.created_by))
        .where(
            events.c.id.in_(member_event_ids),
            events.c.moderation_state != "removed",
        )
    )


def _help_requests_select_for_followed(followed_user_ids: list[UUID]):
    if not followed_user_ids:
        return None
    return (
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
            literal("following").label("feed_source"),
        )
        .select_from(help_requests.outerjoin(users, users.c.id == help_requests.c.author_id))
        .where(
            help_requests.c.author_id.in_(followed_user_ids),
            help_requests.c.moderation_state != "removed",
        )
    )
