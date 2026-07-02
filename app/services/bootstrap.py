from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, literal, not_, or_, select, union_all
from sqlalchemy.orm import Session

from app.services.content import format_schedule_rail_label
from app.models import (
    channels,
    communities,
    conversation_members,
    conversations,
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_edit_request_votes,
    event_edit_requests,
    event_memberships,
    event_phase_change_votes,
    event_phase_change_requests,
    event_plan_votes,
    event_plans,
    event_update_request_votes,
    event_update_requests,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_request_tags,
    help_requests,
    notifications,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_votes,
    project_plans,
    project_service_requests,
    project_update_request_votes,
    project_update_requests,
    projects,
    scope_memberships,
    user_follows,
    users,
)
from app.services.content import _help_request_role_summaries, _load_help_request_roles
from app.services.feeds import _truncate_update_body
from app.services.messages import find_direct_conversation_between, get_total_unread_message_count


def get_onboarding(db: Session) -> dict[str, object]:
    return {
        "title": "Login",
        "intro": "Sign in to post, follow people, and create projects, threads, and events.",
        "accountModes": [
            {
                "value": "signup",
                "label": "Sign up",
                "description": "Create a new account.",
            },
            {
                "value": "login",
                "label": "Log in",
                "description": "Use an existing account.",
            },
        ],
        "starterChannels": [],
        "starterCommunities": [],
    }


def _get_viewer_row(db: Session, current_user_id: UUID):
    row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
            users.c.id == current_user_id
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viewer not found")
    return row


def _get_unread_notification_count(db: Session, current_user_id: UUID) -> int:
    count = db.execute(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.recipient_id == current_user_id,
            notifications.c.is_unread.is_(True),
        )
    ).scalar_one()
    return int(count or 0)


def _get_unread_message_count(db: Session, current_user_id: UUID) -> int:
    return get_total_unread_message_count(db, current_user_id)


def _get_platform_directory_item(db: Session) -> dict[str, object] | None:
    row = db.execute(
        select(channels.c.slug, channels.c.name)
        .where(channels.c.slug.in_(["platform", "stewardship"]))
        .order_by(channels.c.slug.asc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    return {
        "slug": row["slug"],
        "label": row["name"],
        "href": "/platform",
        "visibility": "public",
    }


def _get_channel_directory_items(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(
            scope_memberships.join(channels, channels.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "channel",
        )
        .order_by(channels.c.name.asc())
    ).mappings().all()

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/channels/{row['slug']}",
            "visibility": "public",
        }
        for row in rows
    ]


def _get_community_directory_items(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(communities.c.slug, communities.c.name, communities.c.join_policy)
        .select_from(
            scope_memberships.join(communities, communities.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "community",
        )
        .order_by(communities.c.name.asc())
    ).mappings().all()

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/communities/{row['slug']}",
            "visibility": "private" if row["join_policy"] == "closed" else "public",
        }
        for row in rows
    ]


def _get_suggested_contacts(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    followed_subquery = (
        select(user_follows.c.followed_id)
        .where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
        .subquery("followed_users")
    )

    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
        .where(
            users.c.is_active.is_(True),
            users.c.id != current_user_id,
            not_(users.c.id.in_(select(followed_subquery.c.followed_id))),
        )
        .order_by(users.c.username.asc())
        .limit(8)
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "username": row["username"],
            "bio": row["bio"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]


def _small_iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def _build_activity_rail(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    # ── Scheduled activities from projects the user is a member of ──
    proj_activity_rows = db.execute(
        select(
            project_activities.c.id, project_activities.c.title, project_activities.c.scheduled_at,
            project_activities.c.location_label,
            projects.c.slug.label("parent_slug"), projects.c.title.label("parent_title"),
            projects.c.project_mode,
        )
        .select_from(
            project_memberships.join(projects, projects.c.id == project_memberships.c.project_id)
            .join(project_activities, project_activities.c.project_id == projects.c.id)
        )
        .where(
            project_memberships.c.user_id == current_user_id,
            projects.c.is_closed.is_(False),
            projects.c.current_phase_id == "phase-5",
        )
        .order_by(project_activities.c.scheduled_at.asc())
        .limit(4)
    ).mappings().all()

    if proj_activity_rows:
        activity_ids = [r["id"] for r in proj_activity_rows]
        # Count assignments per activity
        signup_rows = db.execute(
            select(project_activity_roles.c.activity_id, func.count(project_activity_assignments.c.user_id))
            .select_from(
                project_activity_roles.outerjoin(
                    project_activity_assignments,
                    project_activity_assignments.c.role_id == project_activity_roles.c.id,
                )
            )
            .where(project_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(project_activity_roles.c.activity_id)
        ).all()
        signups = {str(aid): int(cnt) for aid, cnt in signup_rows}

        # Sum minimum required per activity
        min_rows = db.execute(
            select(project_activity_roles.c.activity_id, func.sum(project_activity_roles.c.required_count))
            .where(project_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(project_activity_roles.c.activity_id)
        ).all()
        minimums = {str(aid): int(s) for aid, s in min_rows}

        for r in proj_activity_rows:
            aid = str(r["id"])
            signed = signups.get(aid, 0)
            needed = minimums.get(aid, 0)
            items.append({
                "kind": "project",
                "id": aid,
                "subjectId": r["parent_slug"],
                "title": r["title"],
                "href": f"/projects/{r['parent_slug']}?activity={aid}",
                "meta": r["parent_title"],
                "createdAt": _small_iso(r["scheduled_at"]),
                "timeLabel": format_schedule_rail_label(r["scheduled_at"]) if r["scheduled_at"] else "",
                "countLabel": f"{signed} signed up · {needed} needed" if needed > 0 else f"{signed} signed up",
                "projectMode": r["project_mode"],
                "projectSlug": r["parent_slug"],
                "activityId": aid,
            })

    # ── Scheduled activities from events the user is a member of ──
    evt_activity_rows = db.execute(
        select(
            event_activities.c.id, event_activities.c.title, event_activities.c.scheduled_at,
            event_activities.c.location_label,
            events.c.slug.label("parent_slug"), events.c.title.label("parent_title"),
        )
        .select_from(
            event_memberships.join(events, events.c.id == event_memberships.c.event_id)
            .join(event_activities, event_activities.c.event_id == events.c.id)
        )
        .where(
            event_memberships.c.user_id == current_user_id,
            events.c.current_phase_id.in_(["event-plan", "activity"]),
        )
        .order_by(event_activities.c.scheduled_at.asc())
        .limit(4)
    ).mappings().all()

    if evt_activity_rows:
        activity_ids = [r["id"] for r in evt_activity_rows]
        signup_rows = db.execute(
            select(event_activity_roles.c.activity_id, func.count(event_activity_assignments.c.user_id))
            .select_from(
                event_activity_roles.outerjoin(
                    event_activity_assignments,
                    event_activity_assignments.c.role_id == event_activity_roles.c.id,
                )
            )
            .where(event_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(event_activity_roles.c.activity_id)
        ).all()
        signups = {str(aid): int(cnt) for aid, cnt in signup_rows}

        min_rows = db.execute(
            select(event_activity_roles.c.activity_id, func.sum(event_activity_roles.c.required_count))
            .where(event_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(event_activity_roles.c.activity_id)
        ).all()
        minimums = {str(aid): int(s) for aid, s in min_rows}

        for r in evt_activity_rows:
            aid = str(r["id"])
            signed = signups.get(aid, 0)
            needed = minimums.get(aid, 0)
            items.append({
                "kind": "event",
                "id": aid,
                "subjectId": r["parent_slug"],
                "title": r["title"],
                "href": f"/events/{r['parent_slug']}?activity={aid}",
                "meta": r["parent_title"],
                "createdAt": _small_iso(r["scheduled_at"]),
                "timeLabel": format_schedule_rail_label(r["scheduled_at"]) if r["scheduled_at"] else "",
                "countLabel": f"{signed} signed up · {needed} needed" if needed > 0 else f"{signed} signed up",
                "eventSlug": r["parent_slug"],
                "activityId": aid,
            })

    # ── Help requests: author-owned, viewer signups, and open requests in member scopes ──
    help_request_items: list[dict[str, object]] = []
    author_owned_ids: set[UUID] = set()

    author_rows = db.execute(
        select(
            help_requests.c.id,
            help_requests.c.title,
            help_requests.c.body,
            help_requests.c.needed_at,
            help_requests.c.schedule_label,
        )
        .where(
            help_requests.c.author_id == current_user_id,
            help_requests.c.needed_at > datetime.now(timezone.utc),
        )
        .order_by(help_requests.c.needed_at.asc())
        .limit(6)
    ).mappings().all()

    if author_rows:
        author_owned_ids = {row["id"] for row in author_rows}
        author_hr_ids = [row["id"] for row in author_rows]
        author_roles = _load_help_request_roles(db, author_hr_ids, current_user_id)
        for row in author_rows:
            hr_id = str(row["id"])
            roles = author_roles.get(hr_id, [])
            signed, needed = _help_request_role_summaries(roles)
            help_request_items.append({
                "kind": "help-request-owned",
                "id": hr_id,
                "subjectId": hr_id,
                "title": row["title"],
                "href": f"/help-requests/{hr_id}",
                "meta": "Your request",
                "createdAt": _small_iso(row["needed_at"]),
                "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                "countLabel": f"{signed} signed up · {needed} needed" if needed > 0 else f"{signed} signed up",
                "viewerIsAuthor": True,
                "body": _truncate_update_body(str(row["body"] or "")),
            })

    signup_rows = db.execute(
        select(
            help_requests.c.id,
            help_requests.c.title,
            help_requests.c.body,
            help_requests.c.needed_at,
            help_requests.c.schedule_label,
        )
        .select_from(
            help_request_role_assignments.join(
                help_request_roles,
                help_request_roles.c.id == help_request_role_assignments.c.role_id,
            ).join(help_requests, help_requests.c.id == help_request_roles.c.help_request_id)
        )
        .where(help_request_role_assignments.c.user_id == current_user_id)
        .distinct()
        .order_by(help_requests.c.needed_at.asc())
        .limit(6)
    ).mappings().all()

    signed_up_ids: set[UUID] = set()
    if signup_rows:
        signup_hr_ids = [row["id"] for row in signup_rows]
        signed_up_ids = set(signup_hr_ids)
        signup_roles = _load_help_request_roles(db, signup_hr_ids, current_user_id)
        for row in signup_rows:
            if row["id"] in author_owned_ids:
                continue
            hr_id = str(row["id"])
            roles = signup_roles.get(hr_id, [])
            signed, needed = _help_request_role_summaries(roles)
            help_request_items.append({
                "kind": "help-request-signup",
                "id": hr_id,
                "subjectId": hr_id,
                "title": row["title"],
                "href": f"/help-requests/{hr_id}",
                "meta": "You signed up",
                "createdAt": _small_iso(row["needed_at"]),
                "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                "countLabel": f"{signed} signed up · {needed} needed" if needed > 0 else f"{signed} signed up",
                "body": _truncate_update_body(str(row["body"] or "")),
            })

    member_scope_rows = db.execute(
        select(scope_memberships.c.scope_kind, scope_memberships.c.scope_id).where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_id.is_not(None),
        )
    ).all()
    member_channel_ids = [scope_id for kind, scope_id in member_scope_rows if kind == "channel"]
    member_community_ids = [scope_id for kind, scope_id in member_scope_rows if kind == "community"]

    if member_channel_ids or member_community_ids:
        tag_conditions = []
        if member_channel_ids:
            tag_conditions.append(help_request_tags.c.channel_id.in_(member_channel_ids))
        if member_community_ids:
            tag_conditions.append(help_request_tags.c.community_id.in_(member_community_ids))

        open_query = (
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.body,
                help_requests.c.needed_at,
                help_requests.c.schedule_label,
            )
            .select_from(
                help_requests.join(
                    help_request_tags,
                    help_request_tags.c.help_request_id == help_requests.c.id,
                )
            )
            .where(
                help_requests.c.needed_at > datetime.now(timezone.utc),
                or_(*tag_conditions),
            )
            .distinct()
            .order_by(help_requests.c.needed_at.asc())
            .limit(6)
        )
        if signed_up_ids:
            open_query = open_query.where(help_requests.c.id.not_in(list(signed_up_ids)))

        open_rows = db.execute(open_query).mappings().all()
        if open_rows:
            open_hr_ids = [row["id"] for row in open_rows]
            open_roles = _load_help_request_roles(db, open_hr_ids, current_user_id)
            for row in open_rows:
                if row["id"] in author_owned_ids:
                    continue
                hr_id = str(row["id"])
                roles = open_roles.get(hr_id, [])
                signed, needed = _help_request_role_summaries(roles)
                help_request_items.append({
                    "kind": "help-request-open",
                    "id": f"open-{hr_id}",
                    "subjectId": hr_id,
                    "title": row["title"],
                    "href": f"/help-requests/{hr_id}",
                    "meta": "",
                    "createdAt": _small_iso(row["needed_at"]),
                    "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                    "countLabel": f"{signed} signed up · {needed} needed" if needed > 0 else f"{signed} signed up",
                    "body": _truncate_update_body(str(row["body"] or "")),
                })

    items.extend(help_request_items)

    # ── Active votes: open requests where user is a member and hasn't voted yet ──
    vote_items: list[dict[str, object]] = []
    limit_per_type = 3

    # Helper: query yes/no counts for a set of request IDs from a vote table
    def _vote_counts(vote_table, id_col, request_ids: list) -> dict[str, dict[str, int]]:
        if not request_ids:
            return {}
        rows = db.execute(
            select(id_col, vote_table.c.vote, func.count())
            .where(id_col.in_(request_ids))
            .group_by(id_col, vote_table.c.vote)
        ).all()
        result: dict[str, dict[str, int]] = {}
        for rid, vote, cnt in rows:
            result.setdefault(str(rid), {"yes": 0, "no": 0})
            result[str(rid)][str(vote)] = int(cnt)
        return result

    def _build_count_label(yes: int, no: int) -> str:
        return f"{yes} yes / {no} no"

    def _vote_href(surface: str, slug: str, vote_kind: str, target_id: object) -> str:
        return f"/{surface}/{slug}?open=vote&voteKind={vote_kind}&voteTarget={target_id}"

    # ── Open service requests the user can review ──
    request_rows = db.execute(
        select(
            project_service_requests.c.id,
            project_service_requests.c.created_at,
            project_service_requests.c.title,
            project_service_requests.c.body,
            project_service_requests.c.scheduled_at,
            project_service_requests.c.requester_id,
            projects.c.slug,
            projects.c.title.label("project_title"),
            projects.c.project_mode,
            users.c.username.label("requester_username"),
        )
        .select_from(
            project_service_requests.join(projects, projects.c.id == project_service_requests.c.project_id)
            .outerjoin(
                project_memberships,
                and_(
                    project_memberships.c.project_id == projects.c.id,
                    project_memberships.c.user_id == current_user_id,
                ),
            )
            .outerjoin(users, users.c.id == project_service_requests.c.requester_id)
        )
        .where(
            project_service_requests.c.status == "open",
            or_(
                and_(
                    project_memberships.c.user_id == current_user_id,
                    project_memberships.c.is_manager.is_(True),
                ),
                and_(
                    projects.c.project_mode == "personal-service",
                    projects.c.author_id == current_user_id,
                ),
            ),
        )
        .order_by(project_service_requests.c.created_at.desc())
        .limit(6)
    ).mappings().all()

    for r in request_rows:
        request_id = str(r["id"])
        requester = r["requester_username"] or "Unknown requester"
        conversation_id: str | None = None
        if r["project_mode"] == "personal-service" and r["requester_id"] is not None:
            direct_conversation = find_direct_conversation_between(db, current_user_id, r["requester_id"])
            if direct_conversation is not None:
                conversation_id = str(direct_conversation["id"])

        href = f"/projects/{r['slug']}?request={request_id}"
        items.append({
            "kind": "request",
            "id": request_id,
            "subjectId": r["slug"],
            "title": r["title"],
            "href": href,
            "meta": r["project_title"],
            "createdAt": _small_iso(r["created_at"]),
            "timeLabel": _small_iso(r["scheduled_at"]),
            "countLabel": f"Requested by {requester}",
            "projectMode": r["project_mode"],
            "projectSlug": r["slug"],
            "requestId": request_id,
            "requesterUsername": requester,
            "conversationId": conversation_id,
        })

    # Project phase change requests
    rows = db.execute(
        select(project_phase_change_requests.c.id, project_phase_change_requests.c.created_at,
               projects.c.slug, projects.c.title, project_phase_change_requests.c.target_phase_id)
        .select_from(
            project_phase_change_requests.join(
                projects, projects.c.id == project_phase_change_requests.c.project_id
            ).join(
                project_memberships,
                and_(project_memberships.c.project_id == projects.c.id,
                     project_memberships.c.user_id == current_user_id),
            ).outerjoin(
                project_phase_change_votes,
                and_(project_phase_change_votes.c.request_id == project_phase_change_requests.c.id,
                     project_phase_change_votes.c.voter_id == current_user_id),
            )
        )
        .where(project_phase_change_requests.c.status == "open",
               project_phase_change_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(project_phase_change_votes, project_phase_change_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": f"Phase change: {r['title']}",
                "href": _vote_href("projects", r["slug"], "phase_change", r["id"]),
                "meta": f"Phase change → {r['target_phase_id']}",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "project", "voteKindLabel": "phase_change",
                "voteTargetId": str(r["id"])})

    # Project plans
    rows = db.execute(
        select(project_plans.c.id, project_plans.c.created_at,
               projects.c.slug, projects.c.title, project_plans.c.title.label("plan_title"))
        .select_from(
            project_plans.join(projects, projects.c.id == project_plans.c.project_id)
            .join(project_memberships, and_(project_memberships.c.project_id == projects.c.id,
                                             project_memberships.c.user_id == current_user_id))
            .outerjoin(project_plan_votes, and_(project_plan_votes.c.plan_id == project_plans.c.id,
                                                  project_plan_votes.c.voter_id == current_user_id))
        )
        .where(project_plans.c.status == "open", project_plan_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(project_plan_votes, project_plan_votes.c.plan_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("projects", r["slug"], "plan", r["id"]),
                "meta": f"Plan vote",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "project", "voteKindLabel": "plan",
                "voteTargetId": str(r["id"])})

    # Project update requests
    rows = db.execute(
        select(project_update_requests.c.id, project_update_requests.c.created_at,
               projects.c.slug, projects.c.title, project_update_requests.c.body)
        .select_from(
            project_update_requests.join(projects, projects.c.id == project_update_requests.c.project_id)
            .join(project_memberships, and_(project_memberships.c.project_id == projects.c.id,
                                             project_memberships.c.user_id == current_user_id))
            .outerjoin(project_update_request_votes,
                       and_(project_update_request_votes.c.request_id == project_update_requests.c.id,
                            project_update_request_votes.c.voter_id == current_user_id))
        )
        .where(project_update_requests.c.status == "open", project_update_request_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(project_update_request_votes, project_update_request_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("projects", r["slug"], "update", r["id"]),
                "meta": "Update request",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "project", "voteKindLabel": "update",
                "voteTargetId": str(r["id"])})

    # Project edit requests
    rows = db.execute(
        select(project_edit_requests.c.id, project_edit_requests.c.created_at,
               projects.c.slug, projects.c.title)
        .select_from(
            project_edit_requests.join(projects, projects.c.id == project_edit_requests.c.project_id)
            .join(project_memberships, and_(project_memberships.c.project_id == projects.c.id,
                                             project_memberships.c.user_id == current_user_id))
            .outerjoin(project_edit_request_votes,
                       and_(project_edit_request_votes.c.request_id == project_edit_requests.c.id,
                            project_edit_request_votes.c.voter_id == current_user_id))
        )
        .where(project_edit_requests.c.status == "open", project_edit_request_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(project_edit_request_votes, project_edit_request_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("projects", r["slug"], "edit", r["id"]),
                "meta": "Edit request",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "project", "voteKindLabel": "edit",
                "voteTargetId": str(r["id"])})

    # Event phase change requests
    rows = db.execute(
        select(event_phase_change_requests.c.id, event_phase_change_requests.c.created_at,
               events.c.slug, events.c.title, event_phase_change_requests.c.target_phase_id)
        .select_from(
            event_phase_change_requests.join(events, events.c.id == event_phase_change_requests.c.event_id)
            .join(event_memberships, and_(event_memberships.c.event_id == events.c.id,
                                           event_memberships.c.user_id == current_user_id))
            .outerjoin(event_phase_change_votes,
                       and_(event_phase_change_votes.c.request_id == event_phase_change_requests.c.id,
                            event_phase_change_votes.c.voter_id == current_user_id))
        )
        .where(event_phase_change_requests.c.status == "open", event_phase_change_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(event_phase_change_votes, event_phase_change_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": f"Phase change: {r['title']}",
                "href": _vote_href("events", r["slug"], "phase_change", r["id"]),
                "meta": f"Phase change → {r['target_phase_id']}",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "event", "voteKindLabel": "phase_change",
                "voteTargetId": str(r["id"])})

    # Event plans
    rows = db.execute(
        select(event_plans.c.id, event_plans.c.created_at,
               events.c.slug, events.c.title, event_plans.c.title.label("plan_title"))
        .select_from(
            event_plans.join(events, events.c.id == event_plans.c.event_id)
            .join(event_memberships, and_(event_memberships.c.event_id == events.c.id,
                                           event_memberships.c.user_id == current_user_id))
            .outerjoin(event_plan_votes, and_(event_plan_votes.c.plan_id == event_plans.c.id,
                                                event_plan_votes.c.voter_id == current_user_id))
        )
        .where(event_plans.c.status == "open", event_plan_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(event_plan_votes, event_plan_votes.c.plan_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("events", r["slug"], "plan", r["id"]),
                "meta": "Plan vote",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "event", "voteKindLabel": "plan",
                "voteTargetId": str(r["id"])})

    # Event update requests
    rows = db.execute(
        select(event_update_requests.c.id, event_update_requests.c.created_at,
               events.c.slug, events.c.title)
        .select_from(
            event_update_requests.join(events, events.c.id == event_update_requests.c.event_id)
            .join(event_memberships, and_(event_memberships.c.event_id == events.c.id,
                                           event_memberships.c.user_id == current_user_id))
            .outerjoin(event_update_request_votes,
                       and_(event_update_request_votes.c.request_id == event_update_requests.c.id,
                            event_update_request_votes.c.voter_id == current_user_id))
        )
        .where(event_update_requests.c.status == "open", event_update_request_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(event_update_request_votes, event_update_request_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("events", r["slug"], "update", r["id"]),
                "meta": "Update request",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "event", "voteKindLabel": "update",
                "voteTargetId": str(r["id"])})

    # Event edit requests
    rows = db.execute(
        select(event_edit_requests.c.id, event_edit_requests.c.created_at,
               events.c.slug, events.c.title)
        .select_from(
            event_edit_requests.join(events, events.c.id == event_edit_requests.c.event_id)
            .join(event_memberships, and_(event_memberships.c.event_id == events.c.id,
                                           event_memberships.c.user_id == current_user_id))
            .outerjoin(event_edit_request_votes,
                       and_(event_edit_request_votes.c.request_id == event_edit_requests.c.id,
                            event_edit_request_votes.c.voter_id == current_user_id))
        )
        .where(event_edit_requests.c.status == "open", event_edit_request_votes.c.voter_id.is_(None))
        .limit(limit_per_type)
    ).mappings().all()
    if rows:
        counts = _vote_counts(event_edit_request_votes, event_edit_request_votes.c.request_id,
                              [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append({"kind": "vote", "id": str(r["id"]),
                "title": r['title'],
                "href": _vote_href("events", r["slug"], "edit", r["id"]),
                "meta": "Edit request",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": _build_count_label(c["yes"], c["no"]),
                "voteEntityKind": "event", "voteKindLabel": "edit",
                "voteTargetId": str(r["id"])})

    # Limit total vote items to 8, newest first (by created_at, fallback to id)
    vote_items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    items.extend(vote_items[:8])

    return items


def get_bootstrap_summary(db: Session, current_user_id: UUID) -> dict[str, object]:
    return {
        "unreadCounts": {
            "notifications": _get_unread_notification_count(db, current_user_id),
            "messages": _get_unread_message_count(db, current_user_id),
        },
    }


def get_bootstrap(db: Session, current_user_id: UUID) -> dict[str, object]:
    viewer = _get_viewer_row(db, current_user_id)

    return {
        "viewer": {
            "id": viewer["id"],
            "username": viewer["username"],
            "bio": viewer["bio"],
            "profileImageUrl": viewer["profile_image_url"],
        },
        "featureFlags": {
            "assets": False,
            "funding": False,
            "platform": True,
        },
        "unreadCounts": {
            "notifications": _get_unread_notification_count(db, current_user_id),
            "messages": _get_unread_message_count(db, current_user_id),
        },
        "directory": {
            "platform": _get_platform_directory_item(db),
            "channels": _get_channel_directory_items(db, current_user_id),
            "communities": _get_community_directory_items(db, current_user_id),
        },
        "suggestedContacts": _get_suggested_contacts(db, current_user_id),
        "activityRail": _build_activity_rail(db, current_user_id),
    }
