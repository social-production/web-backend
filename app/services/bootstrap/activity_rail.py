from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    detail_link_request_votes,
    detail_link_requests,
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_edit_request_votes,
    event_edit_requests,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_criterion_ratings,
    event_plan_votes,
    event_plans,
    event_update_request_votes,
    event_update_requests,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_request_tags,
    help_requests,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_request_votes,
    project_edit_requests,
    project_memberships,
    project_merge_capability_change_requests,
    project_merge_capability_change_votes,
    project_merge_capability_members,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_criterion_ratings,
    project_plan_votes,
    project_plans,
    project_pull_request_votes,
    project_pull_requests,
    project_repository_replacement_requests,
    project_repository_replacement_votes,
    project_service_requests,
    project_update_request_votes,
    project_update_requests,
    projects,
    scope_memberships,
    users,
)
from app.services.bootstrap.summary import _small_iso
from app.services.content import _help_request_role_summaries, _load_help_request_roles
from app.services.feeds import _truncate_update_body
from app.services.messages import find_direct_conversation_between


def _build_activity_rail(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    now = datetime.now(UTC)

    # ── Scheduled activities from projects the user is a member of ──
    proj_activity_rows = (
        db.execute(
            select(
                project_activities.c.id,
                project_activities.c.title,
                project_activities.c.scheduled_at,
                project_activities.c.ends_at,
                project_activities.c.location_label,
                projects.c.slug.label("parent_slug"),
                projects.c.title.label("parent_title"),
                projects.c.project_mode,
            )
            .select_from(
                project_memberships.join(
                    projects, projects.c.id == project_memberships.c.project_id
                ).join(project_activities, project_activities.c.project_id == projects.c.id)
            )
            .where(
                project_memberships.c.user_id == current_user_id,
                projects.c.is_closed.is_(False),
                projects.c.current_phase_id == "phase-5",
                project_activities.c.ends_at > now,
            )
            .order_by(project_activities.c.scheduled_at.asc())
            .limit(4)
        )
        .mappings()
        .all()
    )

    if proj_activity_rows:
        activity_ids = [r["id"] for r in proj_activity_rows]
        # Count assignments per activity
        signup_rows = db.execute(
            select(
                project_activity_roles.c.activity_id,
                func.count(project_activity_assignments.c.user_id),
            )
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
            select(
                project_activity_roles.c.activity_id,
                func.sum(project_activity_roles.c.required_count),
            )
            .where(project_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(project_activity_roles.c.activity_id)
        ).all()
        minimums = {str(aid): int(s) for aid, s in min_rows}

        for r in proj_activity_rows:
            aid = str(r["id"])
            signed = signups.get(aid, 0)
            needed = minimums.get(aid, 0)
            items.append(
                {
                    "kind": "project",
                    "id": aid,
                    "subjectId": r["parent_slug"],
                    "title": r["title"],
                    "href": f"/projects/{r['parent_slug']}?activity={aid}",
                    "meta": r["parent_title"],
                    "createdAt": _small_iso(r["scheduled_at"]),
                    "scheduledAt": _small_iso(r["scheduled_at"]),
                    "endsAt": _small_iso(r["ends_at"]),
                    "countLabel": f"{signed} signed up · {needed} needed"
                    if needed > 0
                    else f"{signed} signed up",
                    "projectMode": r["project_mode"],
                    "projectSlug": r["parent_slug"],
                    "activityId": aid,
                }
            )

    # ── Scheduled activities from events the user is a member of ──
    evt_activity_rows = (
        db.execute(
            select(
                event_activities.c.id,
                event_activities.c.title,
                event_activities.c.scheduled_at,
                event_activities.c.ends_at,
                event_activities.c.location_label,
                events.c.slug.label("parent_slug"),
                events.c.title.label("parent_title"),
            )
            .select_from(
                event_memberships.join(events, events.c.id == event_memberships.c.event_id).join(
                    event_activities, event_activities.c.event_id == events.c.id
                )
            )
            .where(
                event_memberships.c.user_id == current_user_id,
                events.c.current_phase_id.in_(["event-plan", "activity"]),
                event_activities.c.ends_at > now,
            )
            .order_by(event_activities.c.scheduled_at.asc())
            .limit(4)
        )
        .mappings()
        .all()
    )

    if evt_activity_rows:
        activity_ids = [r["id"] for r in evt_activity_rows]
        signup_rows = db.execute(
            select(
                event_activity_roles.c.activity_id, func.count(event_activity_assignments.c.user_id)
            )
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
            select(
                event_activity_roles.c.activity_id, func.sum(event_activity_roles.c.required_count)
            )
            .where(event_activity_roles.c.activity_id.in_(activity_ids))
            .group_by(event_activity_roles.c.activity_id)
        ).all()
        minimums = {str(aid): int(s) for aid, s in min_rows}

        for r in evt_activity_rows:
            aid = str(r["id"])
            signed = signups.get(aid, 0)
            needed = minimums.get(aid, 0)
            items.append(
                {
                    "kind": "event",
                    "id": aid,
                    "subjectId": r["parent_slug"],
                    "title": r["title"],
                    "href": f"/events/{r['parent_slug']}?activity={aid}",
                    "meta": r["parent_title"],
                    "createdAt": _small_iso(r["scheduled_at"]),
                    "scheduledAt": _small_iso(r["scheduled_at"]),
                    "endsAt": _small_iso(r["ends_at"]),
                    "countLabel": f"{signed} signed up · {needed} needed"
                    if needed > 0
                    else f"{signed} signed up",
                    "eventSlug": r["parent_slug"],
                    "activityId": aid,
                }
            )

    # ── Help requests: author-owned, viewer signups, and open requests in member scopes ──
    help_request_items: list[dict[str, object]] = []
    author_owned_ids: set[UUID] = set()

    author_rows = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.title,
                help_requests.c.body,
                help_requests.c.needed_at,
                help_requests.c.schedule_label,
            )
            .where(
                help_requests.c.author_id == current_user_id,
                or_(
                    help_requests.c.ends_at.is_(None),
                    help_requests.c.ends_at > now,
                ),
            )
            .order_by(help_requests.c.needed_at.asc())
            .limit(6)
        )
        .mappings()
        .all()
    )

    if author_rows:
        author_owned_ids = {row["id"] for row in author_rows}
        author_hr_ids = [row["id"] for row in author_rows]
        author_roles = _load_help_request_roles(db, author_hr_ids, current_user_id)
        for row in author_rows:
            hr_id = str(row["id"])
            roles = author_roles.get(hr_id, [])
            signed, needed = _help_request_role_summaries(roles)
            help_request_items.append(
                {
                    "kind": "help-request-owned",
                    "id": hr_id,
                    "subjectId": hr_id,
                    "title": row["title"],
                    "href": f"/help-requests/{hr_id}",
                    "meta": "Your request",
                    "createdAt": _small_iso(row["needed_at"]),
                    "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                    "countLabel": f"{signed} signed up · {needed} needed"
                    if needed > 0
                    else f"{signed} signed up",
                    "viewerIsAuthor": True,
                    "body": _truncate_update_body(str(row["body"] or "")),
                }
            )

    signup_rows = (
        db.execute(
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
            .where(
                help_request_role_assignments.c.user_id == current_user_id,
                or_(
                    help_requests.c.ends_at.is_(None),
                    help_requests.c.ends_at > now,
                ),
            )
            .distinct()
            .order_by(help_requests.c.needed_at.asc())
            .limit(6)
        )
        .mappings()
        .all()
    )

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
            help_request_items.append(
                {
                    "kind": "help-request-signup",
                    "id": hr_id,
                    "subjectId": hr_id,
                    "title": row["title"],
                    "href": f"/help-requests/{hr_id}",
                    "meta": "You signed up",
                    "createdAt": _small_iso(row["needed_at"]),
                    "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                    "countLabel": f"{signed} signed up · {needed} needed"
                    if needed > 0
                    else f"{signed} signed up",
                    "body": _truncate_update_body(str(row["body"] or "")),
                }
            )

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
                or_(
                    help_requests.c.ends_at.is_(None),
                    help_requests.c.ends_at > now,
                ),
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
                help_request_items.append(
                    {
                        "kind": "help-request-open",
                        "id": f"open-{hr_id}",
                        "subjectId": hr_id,
                        "title": row["title"],
                        "href": f"/help-requests/{hr_id}",
                        "meta": "",
                        "createdAt": _small_iso(row["needed_at"]),
                        "timeLabel": row["schedule_label"] or _small_iso(row["needed_at"]),
                        "countLabel": f"{signed} signed up · {needed} needed"
                        if needed > 0
                        else f"{signed} signed up",
                        "body": _truncate_update_body(str(row["body"] or "")),
                    }
                )

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

    def _vote_href(
        surface: str,
        slug: str,
        vote_kind: str,
        target_id: object,
        *,
        assess: bool = False,
    ) -> str:
        href = f"/{surface}/{slug}?open=vote&voteKind={vote_kind}&voteTarget={target_id}"
        if assess:
            href += "&assess=1"
        return href

    # ── Open service requests the user can review ──
    request_rows = (
        db.execute(
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
                project_service_requests.join(
                    projects, projects.c.id == project_service_requests.c.project_id
                )
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
        )
        .mappings()
        .all()
    )

    for r in request_rows:
        request_id = str(r["id"])
        requester = r["requester_username"] or "Unknown requester"
        conversation_id: str | None = None
        if r["project_mode"] == "personal-service" and r["requester_id"] is not None:
            direct_conversation = find_direct_conversation_between(
                db, current_user_id, r["requester_id"]
            )
            if direct_conversation is not None:
                conversation_id = str(direct_conversation["id"])

        href = f"/projects/{r['slug']}?request={request_id}"
        items.append(
            {
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
            }
        )

    # Project phase change requests
    rows = (
        db.execute(
            select(
                project_phase_change_requests.c.id,
                project_phase_change_requests.c.created_at,
                projects.c.slug,
                projects.c.title,
                project_phase_change_requests.c.target_phase_id,
            )
            .select_from(
                project_phase_change_requests.join(
                    projects, projects.c.id == project_phase_change_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_phase_change_votes,
                    and_(
                        project_phase_change_votes.c.request_id
                        == project_phase_change_requests.c.id,
                        project_phase_change_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                project_phase_change_requests.c.status == "open",
                project_phase_change_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_phase_change_votes,
            project_phase_change_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": f"Phase change: {r['title']}",
                    "href": _vote_href("projects", r["slug"], "phase_change", r["id"]),
                    "meta": f"Advance to {str(r['target_phase_id']).replace('-', ' ').title()}?",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "phase_change",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Project plans
    rows = (
        db.execute(
            select(
                project_plans.c.id,
                project_plans.c.created_at,
                projects.c.slug,
                projects.c.title,
                projects.c.current_phase_id,
                project_plans.c.title.label("plan_title"),
            )
            .select_from(
                project_plans.join(projects, projects.c.id == project_plans.c.project_id)
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_plan_votes,
                    and_(
                        project_plan_votes.c.plan_id == project_plans.c.id,
                        project_plan_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(project_plans.c.status == "open", project_plan_votes.c.voter_id.is_(None))
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_plan_votes, project_plan_votes.c.plan_id, [r["id"] for r in rows]
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            criterion_count = (
                db.execute(
                    select(func.count())
                    .select_from(project_plan_criterion_ratings)
                    .where(
                        project_plan_criterion_ratings.c.plan_id == r["id"],
                        project_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                ).scalar()
                or 0
            )
            phase_id = str(r["current_phase_id"])
            plan_phase_id = phase_id if phase_id in {"phase-2", "phase-3"} else "phase-2"
            vote_sub_kind = "criterion" if int(criterion_count) == 0 else "overall"
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["plan_title"] or r["title"],
                    "href": _vote_href(
                        "projects",
                        r["slug"],
                        "plan",
                        r["id"],
                        assess=vote_sub_kind == "overall",
                    ),
                    "meta": "Assess and approve this plan"
                    if vote_sub_kind == "criterion"
                    else f"Approve “{r['plan_title']}”?",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "plan",
                    "voteTargetId": str(r["id"]),
                    "planPhaseId": plan_phase_id
                    if plan_phase_id in {"phase-2", "phase-3"}
                    else "phase-2",
                    "voteSubKind": vote_sub_kind,
                    "projectSlug": r["slug"],
                }
            )

    # Project update requests
    rows = (
        db.execute(
            select(
                project_update_requests.c.id,
                project_update_requests.c.created_at,
                projects.c.slug,
                projects.c.title,
                project_update_requests.c.body,
            )
            .select_from(
                project_update_requests.join(
                    projects, projects.c.id == project_update_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_update_request_votes,
                    and_(
                        project_update_request_votes.c.request_id == project_update_requests.c.id,
                        project_update_request_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                project_update_requests.c.status == "open",
                project_update_request_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_update_request_votes,
            project_update_request_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["title"],
                    "href": _vote_href("projects", r["slug"], "update", r["id"]),
                    "meta": "Update request",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "update",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Project edit requests
    rows = (
        db.execute(
            select(
                project_edit_requests.c.id,
                project_edit_requests.c.created_at,
                projects.c.slug,
                projects.c.title,
            )
            .select_from(
                project_edit_requests.join(
                    projects, projects.c.id == project_edit_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_edit_request_votes,
                    and_(
                        project_edit_request_votes.c.request_id == project_edit_requests.c.id,
                        project_edit_request_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                project_edit_requests.c.status == "open",
                project_edit_request_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_edit_request_votes,
            project_edit_request_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["title"],
                    "href": _vote_href("projects", r["slug"], "edit", r["id"]),
                    "meta": "Edit request",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "edit",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Event phase change requests
    rows = (
        db.execute(
            select(
                event_phase_change_requests.c.id,
                event_phase_change_requests.c.created_at,
                events.c.slug,
                events.c.title,
                event_phase_change_requests.c.target_phase_id,
            )
            .select_from(
                event_phase_change_requests.join(
                    events, events.c.id == event_phase_change_requests.c.event_id
                )
                .join(
                    event_memberships,
                    and_(
                        event_memberships.c.event_id == events.c.id,
                        event_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    event_phase_change_votes,
                    and_(
                        event_phase_change_votes.c.request_id == event_phase_change_requests.c.id,
                        event_phase_change_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                event_phase_change_requests.c.status == "open",
                event_phase_change_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            event_phase_change_votes, event_phase_change_votes.c.request_id, [r["id"] for r in rows]
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": f"Phase change: {r['title']}",
                    "href": _vote_href("events", r["slug"], "phase_change", r["id"]),
                    "meta": f"Advance to {str(r['target_phase_id']).replace('-', ' ').title()}?",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "event",
                    "voteKindLabel": "phase_change",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Event plans
    rows = (
        db.execute(
            select(
                event_plans.c.id,
                event_plans.c.created_at,
                events.c.slug,
                events.c.title,
                event_plans.c.title.label("plan_title"),
            )
            .select_from(
                event_plans.join(events, events.c.id == event_plans.c.event_id)
                .join(
                    event_memberships,
                    and_(
                        event_memberships.c.event_id == events.c.id,
                        event_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    event_plan_votes,
                    and_(
                        event_plan_votes.c.plan_id == event_plans.c.id,
                        event_plan_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(event_plans.c.status == "open", event_plan_votes.c.voter_id.is_(None))
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(event_plan_votes, event_plan_votes.c.plan_id, [r["id"] for r in rows])
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            criterion_count = (
                db.execute(
                    select(func.count())
                    .select_from(event_plan_criterion_ratings)
                    .where(
                        event_plan_criterion_ratings.c.plan_id == r["id"],
                        event_plan_criterion_ratings.c.voter_id == current_user_id,
                    )
                ).scalar()
                or 0
            )
            vote_sub_kind = "criterion" if int(criterion_count) == 0 else "overall"
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["plan_title"] or r["title"],
                    "href": _vote_href(
                        "events",
                        r["slug"],
                        "plan",
                        r["id"],
                        assess=vote_sub_kind == "overall",
                    ),
                    "meta": "Assess and approve this plan"
                    if vote_sub_kind == "criterion"
                    else f"Approve “{r['plan_title']}”?",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "event",
                    "voteKindLabel": "plan",
                    "voteTargetId": str(r["id"]),
                    "voteSubKind": vote_sub_kind,
                    "eventSlug": r["slug"],
                }
            )

    # Event update requests
    rows = (
        db.execute(
            select(
                event_update_requests.c.id,
                event_update_requests.c.created_at,
                events.c.slug,
                events.c.title,
            )
            .select_from(
                event_update_requests.join(events, events.c.id == event_update_requests.c.event_id)
                .join(
                    event_memberships,
                    and_(
                        event_memberships.c.event_id == events.c.id,
                        event_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    event_update_request_votes,
                    and_(
                        event_update_request_votes.c.request_id == event_update_requests.c.id,
                        event_update_request_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                event_update_requests.c.status == "open",
                event_update_request_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            event_update_request_votes,
            event_update_request_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["title"],
                    "href": _vote_href("events", r["slug"], "update", r["id"]),
                    "meta": "Update request",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "event",
                    "voteKindLabel": "update",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Event edit requests
    rows = (
        db.execute(
            select(
                event_edit_requests.c.id,
                event_edit_requests.c.created_at,
                events.c.slug,
                events.c.title,
            )
            .select_from(
                event_edit_requests.join(events, events.c.id == event_edit_requests.c.event_id)
                .join(
                    event_memberships,
                    and_(
                        event_memberships.c.event_id == events.c.id,
                        event_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    event_edit_request_votes,
                    and_(
                        event_edit_request_votes.c.request_id == event_edit_requests.c.id,
                        event_edit_request_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                event_edit_requests.c.status == "open",
                event_edit_request_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            event_edit_request_votes, event_edit_request_votes.c.request_id, [r["id"] for r in rows]
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["title"],
                    "href": _vote_href("events", r["slug"], "edit", r["id"]),
                    "meta": "Edit request",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "event",
                    "voteKindLabel": "edit",
                    "voteTargetId": str(r["id"]),
                }
            )

    # Open link requests the viewer can vote on
    link_rows = (
        db.execute(
            select(
                detail_link_requests.c.id,
                detail_link_requests.c.created_at,
                detail_link_requests.c.summary,
                detail_link_requests.c.request_type,
                detail_link_requests.c.source_kind,
                detail_link_requests.c.target_kind,
                detail_link_requests.c.source_project_id,
                detail_link_requests.c.source_event_id,
                detail_link_requests.c.target_project_id,
                detail_link_requests.c.target_event_id,
            )
            .where(detail_link_requests.c.status == "open")
            .order_by(detail_link_requests.c.created_at.desc())
            .limit(limit_per_type * 3)
        )
        .mappings()
        .all()
    )
    if link_rows:
        member_project_ids = {
            row[0]
            for row in db.execute(
                select(project_memberships.c.project_id).where(
                    project_memberships.c.user_id == current_user_id
                )
            ).all()
        }
        member_event_ids = {
            row[0]
            for row in db.execute(
                select(event_memberships.c.event_id).where(
                    event_memberships.c.user_id == current_user_id
                )
            ).all()
        }
        counts = _vote_counts(
            detail_link_request_votes,
            detail_link_request_votes.c.request_id,
            [r["id"] for r in link_rows],
        )
        for r in link_rows:
            source_is_project = r["source_kind"] == "project"
            target_is_project = r["target_kind"] == "project"
            source_id = r["source_project_id"] if source_is_project else r["source_event_id"]
            target_id = r["target_project_id"] if target_is_project else r["target_event_id"]
            viewer_on_source = (
                source_id in member_project_ids
                if source_is_project
                else source_id in member_event_ids
            )
            viewer_on_target = (
                target_id in member_project_ids
                if target_is_project
                else target_id in member_event_ids
            )
            if not viewer_on_source and not viewer_on_target:
                continue

            scopes: list[tuple[str, str, object]] = []
            if viewer_on_source:
                scopes.append(
                    (
                        "source",
                        "project" if source_is_project else "event",
                        source_id,
                    )
                )
            if viewer_on_target:
                scopes.append(
                    (
                        "target",
                        "project" if target_is_project else "event",
                        target_id,
                    )
                )

            for vote_scope, entity_kind, entity_id in scopes:
                already_voted = db.execute(
                    select(detail_link_request_votes.c.vote).where(
                        detail_link_request_votes.c.request_id == r["id"],
                        detail_link_request_votes.c.voter_id == current_user_id,
                        detail_link_request_votes.c.vote_scope == vote_scope,
                    )
                ).first()
                if already_voted is not None:
                    continue

                if entity_kind == "project":
                    subject = (
                        db.execute(
                            select(projects.c.slug, projects.c.title).where(
                                projects.c.id == entity_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    surface = "projects"
                else:
                    subject = (
                        db.execute(
                            select(events.c.slug, events.c.title).where(events.c.id == entity_id)
                        )
                        .mappings()
                        .first()
                    )
                    surface = "events"
                if subject is None:
                    continue

                counterpart_kind = r["target_kind"] if vote_scope == "source" else r["source_kind"]
                counterpart_id = target_id if vote_scope == "source" else source_id
                if counterpart_kind == "project":
                    counterpart = (
                        db.execute(select(projects.c.title).where(projects.c.id == counterpart_id))
                        .mappings()
                        .first()
                    )
                else:
                    counterpart = (
                        db.execute(select(events.c.title).where(events.c.id == counterpart_id))
                        .mappings()
                        .first()
                    )
                counterpart_title = counterpart["title"] if counterpart else "linked record"
                c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
                request_type = str(r.get("request_type") or "create")
                meta_prefix = "Sever link" if request_type == "sever" else "Link vote"
                vote_items.append(
                    {
                        "kind": "vote",
                        "id": f"{r['id']}:{vote_scope}",
                        "subjectId": subject["slug"],
                        "title": subject["title"],
                        "href": (f"/{surface}/{subject['slug']}?tab=links&linkRequest={r['id']}"),
                        "meta": f"{meta_prefix} · {counterpart_title}",
                        "createdAt": _small_iso(r["created_at"]),
                        "countLabel": _build_count_label(c["yes"], c["no"]),
                        "voteEntityKind": entity_kind,
                        "voteKindLabel": "link_sever" if request_type == "sever" else "link",
                        "voteTargetId": str(r["id"]),
                        "projectSlug": subject["slug"] if entity_kind == "project" else None,
                        "eventSlug": subject["slug"] if entity_kind == "event" else None,
                        "body": r["summary"],
                    }
                )

    # Project software pull-request votes (approval + confirmation)
    rows = (
        db.execute(
            select(
                project_pull_requests.c.id,
                project_pull_requests.c.created_at,
                project_pull_requests.c.title,
                project_pull_requests.c.summary,
                project_pull_requests.c.stage,
                projects.c.slug,
                projects.c.title.label("project_title"),
            )
            .select_from(
                project_pull_requests.join(
                    projects, projects.c.id == project_pull_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_pull_request_votes,
                    and_(
                        project_pull_request_votes.c.request_id == project_pull_requests.c.id,
                        project_pull_request_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                projects.c.is_closed.is_(False),
                project_pull_requests.c.stage.in_(("approval", "confirmation")),
                project_pull_request_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_pull_request_votes,
            project_pull_request_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            needs_confirmation = r["stage"] == "confirmation"
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["project_title"],
                    "href": _vote_href("projects", r["slug"], "pull_request", r["id"], assess=True),
                    "meta": (
                        f"Merge confirmation · {r['title']}"
                        if needs_confirmation
                        else f"Pull request vote · {r['title']}"
                    ),
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "pull_request",
                    "voteTargetId": str(r["id"]),
                    "body": r["summary"],
                    "projectSlug": r["slug"],
                }
            )

    # Project software merge-needed actions for merge-capable members
    rows = (
        db.execute(
            select(
                project_pull_requests.c.id,
                project_pull_requests.c.created_at,
                project_pull_requests.c.title,
                project_pull_requests.c.summary,
                projects.c.slug,
                projects.c.title.label("project_title"),
            )
            .select_from(
                project_pull_requests.join(
                    projects, projects.c.id == project_pull_requests.c.project_id
                ).join(
                    project_merge_capability_members,
                    and_(
                        project_merge_capability_members.c.project_id == projects.c.id,
                        project_merge_capability_members.c.user_id == current_user_id,
                    ),
                )
            )
            .where(
                projects.c.is_closed.is_(False),
                project_pull_requests.c.stage == "awaiting-merge",
                project_pull_requests.c.merge_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    for r in rows:
        vote_items.append(
            {
                "kind": "vote",
                "id": f"merge:{r['id']}",
                "title": r["project_title"],
                "href": _vote_href("projects", r["slug"], "pull_request_merge", r["id"]),
                "meta": f"Merge needed · {r['title']}",
                "createdAt": _small_iso(r["created_at"]),
                "countLabel": "Record merge",
                "voteEntityKind": "project",
                "voteKindLabel": "pull_request_merge",
                "voteTargetId": str(r["id"]),
                "body": r["summary"],
                "projectSlug": r["slug"],
                "voteSubKind": "criterion",
            }
        )

    # Project merge-capability change votes
    rows = (
        db.execute(
            select(
                project_merge_capability_change_requests.c.id,
                project_merge_capability_change_requests.c.created_at,
                project_merge_capability_change_requests.c.action,
                projects.c.slug,
                projects.c.title.label("project_title"),
                users.c.username.label("target_username"),
            )
            .select_from(
                project_merge_capability_change_requests.join(
                    projects, projects.c.id == project_merge_capability_change_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    users,
                    users.c.id == project_merge_capability_change_requests.c.target_user_id,
                )
                .outerjoin(
                    project_merge_capability_change_votes,
                    and_(
                        project_merge_capability_change_votes.c.request_id
                        == project_merge_capability_change_requests.c.id,
                        project_merge_capability_change_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                projects.c.is_closed.is_(False),
                project_merge_capability_change_requests.c.status == "open",
                project_merge_capability_change_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_merge_capability_change_votes,
            project_merge_capability_change_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            action = "Grant" if r["action"] == "grant" else "Revoke"
            target = r["target_username"] or "member"
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["project_title"],
                    "href": _vote_href("projects", r["slug"], "merge_capability", r["id"]),
                    "meta": f"Merge capability · {action} {target}",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "merge_capability",
                    "voteTargetId": str(r["id"]),
                    "projectSlug": r["slug"],
                }
            )

    # Project repository replacement votes
    rows = (
        db.execute(
            select(
                project_repository_replacement_requests.c.id,
                project_repository_replacement_requests.c.created_at,
                project_repository_replacement_requests.c.repository_url,
                project_repository_replacement_requests.c.reason,
                projects.c.slug,
                projects.c.title.label("project_title"),
            )
            .select_from(
                project_repository_replacement_requests.join(
                    projects, projects.c.id == project_repository_replacement_requests.c.project_id
                )
                .join(
                    project_memberships,
                    and_(
                        project_memberships.c.project_id == projects.c.id,
                        project_memberships.c.user_id == current_user_id,
                    ),
                )
                .outerjoin(
                    project_repository_replacement_votes,
                    and_(
                        project_repository_replacement_votes.c.request_id
                        == project_repository_replacement_requests.c.id,
                        project_repository_replacement_votes.c.voter_id == current_user_id,
                    ),
                )
            )
            .where(
                projects.c.is_closed.is_(False),
                project_repository_replacement_requests.c.status == "open",
                project_repository_replacement_votes.c.voter_id.is_(None),
            )
            .limit(limit_per_type)
        )
        .mappings()
        .all()
    )
    if rows:
        counts = _vote_counts(
            project_repository_replacement_votes,
            project_repository_replacement_votes.c.request_id,
            [r["id"] for r in rows],
        )
        for r in rows:
            c = counts.get(str(r["id"]), {"yes": 0, "no": 0})
            vote_items.append(
                {
                    "kind": "vote",
                    "id": str(r["id"]),
                    "title": r["project_title"],
                    "href": _vote_href("projects", r["slug"], "repository_replacement", r["id"]),
                    "meta": f"Repository replacement · {r['repository_url']}",
                    "createdAt": _small_iso(r["created_at"]),
                    "countLabel": _build_count_label(c["yes"], c["no"]),
                    "voteEntityKind": "project",
                    "voteKindLabel": "repository_replacement",
                    "voteTargetId": str(r["id"]),
                    "body": r["reason"],
                    "projectSlug": r["slug"],
                }
            )

    # Limit total vote items to 8, newest first (by created_at, fallback to id)
    vote_items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    items.extend(vote_items[:8])

    return items


def _viewer_assigned_activity_ids(
    db: Session,
    *,
    project_activity_ids: list[UUID],
    event_activity_ids: list[UUID],
    user_id: UUID,
) -> set[UUID]:
    assigned: set[UUID] = set()

    if project_activity_ids:
        project_rows = db.execute(
            select(project_activity_roles.c.activity_id)
            .select_from(
                project_activity_roles.join(
                    project_activity_assignments,
                    project_activity_assignments.c.role_id == project_activity_roles.c.id,
                )
            )
            .where(
                project_activity_roles.c.activity_id.in_(project_activity_ids),
                project_activity_assignments.c.user_id == user_id,
            )
        ).all()
        assigned.update(row[0] for row in project_rows)

    if event_activity_ids:
        event_rows = db.execute(
            select(event_activity_roles.c.activity_id)
            .select_from(
                event_activity_roles.join(
                    event_activity_assignments,
                    event_activity_assignments.c.role_id == event_activity_roles.c.id,
                )
            )
            .where(
                event_activity_roles.c.activity_id.in_(event_activity_ids),
                event_activity_assignments.c.user_id == user_id,
            )
        ).all()
        assigned.update(row[0] for row in event_rows)

    return assigned
