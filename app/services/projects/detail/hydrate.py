from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_conversions,
    project_edit_request_votes,
    project_edit_requests,
    project_link_request_votes,
    project_link_requests,
    project_links,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_revert_history,
    project_service_request_setting_change_votes,
    project_service_request_setting_changes,
    project_service_request_settings,
    project_service_requests,
    project_signals,
    project_tags,
    project_update_request_votes,
    project_update_requests,
    project_updates,
    project_value_importance_votes,
    project_values,
    projects,
    reports,
    user_follows,
    users,
)
from app.services.access_control import assert_can_view_entity
from app.services.activity_history import (
    build_project_history_items,
    is_activity_ended,
    load_project_ratings_by_activity,
    utc_now,
)
from app.services.content import activity_status_tone
from app.services.detail_links import build_links_frame, empty_links_frame
from app.services.projects.detail.plans import load_project_plans
from app.services.projects.helpers import (
    _build_project_history,
    _get_project_by_slug_row,
    _get_signal_counts,
    _get_signal_counts_db,
    _iso,
    _resolve_effective_project_subtype,
    _username_lookup,
    _visible_lifecycle_phases,
    _vote_summary,
)
from app.services.projects.phases.conversion import (
    CONVERSION_FROM_LABEL,
    CONVERSION_LINK_KIND,
    CONVERSION_TO_LABEL,
    INVENTORY_NOTE,
    PERMANENCE_NOTE,
)
from app.services.projects_plans import _subtype_label
from app.services.projects_software import get_project_software_governance
from app.services.signal_gates import build_proposal_signal_summary
from app.utils.votes import required_votes, resolve_project_vote_population

PROJECT_MODES = frozenset({"productive", "collective-service", "personal-service"})
PROJECT_SUBTYPES = frozenset({"standard", "software"})
PROJECT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
PROJECT_PHASES = (
    ("phase-1", 1, "P1", "Proposal", "Define values and demand."),
    ("phase-2", 2, "P2", "Production Plan", "Select production plan."),
    ("phase-3", 3, "P3", "Distribution Plan", "Select distribution plan."),
    ("phase-4", 4, "P4", "Acquisition", "Prepare acquisition and inventory."),
    ("phase-5", 5, "P5", "Activity", "Run project activities."),
    ("phase-6", 6, "P6", "Pending Execution", "Await execution confirmation."),
    ("phase-7", 7, "P7", "Closed", "Project has closed."),
)


async def get_project_detail(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
    cache: Redis | None = None,
    include_tab_payloads: bool = False,
) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    project_id = row["id"]
    assert_can_view_entity(db, current_user_id, "project", project_id)
    member_count = int(row["member_count"] or 0)
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(row["is_platform_tagged"]),
    )
    uses_platform_vote_context = bool(row["is_platform_tagged"])
    vote_context_label = (
        "Weekly active platform users"
        if uses_platform_vote_context
        else "Weekly active project members"
    )

    if cache is not None:
        try:
            signal_counts = await _get_signal_counts(db, cache, project_id)
        except Exception:
            signal_counts = _get_signal_counts_db(db, project_id)
    else:
        signal_counts = _get_signal_counts_db(db, project_id)

    viewer_membership = None
    if current_user_id is not None:
        viewer_membership = (
            db.execute(
                select(project_memberships).where(
                    project_memberships.c.project_id == project_id,
                    project_memberships.c.user_id == current_user_id,
                )
            )
            .mappings()
            .first()
        )

    viewer_is_member = viewer_membership is not None
    viewer_is_manager = bool(viewer_membership["is_manager"]) if viewer_membership else False
    viewer_is_author = current_user_id is not None and row["author_id"] == current_user_id
    viewer_can_cast_governance = (
        False
        if row["project_mode"] == "personal-service"
        else (current_user_id is not None if uses_platform_vote_context else viewer_is_member)
    )
    viewer_can_review_requests = viewer_is_manager or (
        row["project_mode"] == "personal-service" and viewer_is_author
    )

    author_row = db.execute(select(users.c.username).where(users.c.id == row["author_id"])).first()
    author_username = author_row[0] if author_row else "unknown"

    channel_tag_rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(project_tags.join(channels, channels.c.id == project_tags.c.channel_id))
        .where(project_tags.c.project_id == project_id, project_tags.c.tag_kind == "channel")
    ).all()
    channel_tags = [
        {"slug": slug_v, "label": name, "kind": "channel"} for slug_v, name in channel_tag_rows
    ]

    community_tag_rows = db.execute(
        select(communities.c.slug, communities.c.name)
        .select_from(
            project_tags.join(communities, communities.c.id == project_tags.c.community_id)
        )
        .where(project_tags.c.project_id == project_id, project_tags.c.tag_kind == "community")
    ).all()
    community_tags = [
        {"slug": slug_v, "label": name, "kind": "community"} for slug_v, name in community_tag_rows
    ]

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "project",
                content_votes.c.target_id == project_id,
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    member_rows = db.execute(
        select(
            project_memberships.c.user_id,
            project_memberships.c.is_manager,
            project_memberships.c.is_manager_candidate,
        ).where(project_memberships.c.project_id == project_id)
    ).all()
    member_ids = {member_id for member_id, _, _ in member_rows}

    usernames = _username_lookup(
        db, member_ids | ({row["author_id"]} if row["author_id"] else set())
    )

    members = []
    for member_id, is_manager, is_manager_candidate in member_rows:
        payload = {
            "id": str(member_id),
            "username": usernames.get(member_id, {}).get("username", "unknown"),
            "bio": usernames.get(member_id, {}).get("bio", ""),
        }
        members.append(payload)

    share_contact_rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio)
        .select_from(user_follows.join(users, users.c.id == user_follows.c.followed_id))
        .where(
            user_follows.c.follower_id == row["author_id"],
            user_follows.c.status == "accepted",
        )
        .limit(12)
    ).all()
    share_contacts = [
        {"id": str(user_id), "username": username, "bio": bio or ""}
        for user_id, username, bio in share_contact_rows
    ]

    value_rows = db.execute(
        select(project_values.c.id, project_values.c.label, project_values.c.author_id)
        .where(project_values.c.project_id == project_id)
        .order_by(project_values.c.created_at.asc())
    ).all()
    value_ids = [value_id for value_id, _, _ in value_rows]
    importance_rows = (
        db.execute(
            select(
                project_value_importance_votes.c.value_id,
                project_value_importance_votes.c.voter_id,
                project_value_importance_votes.c.importance,
            ).where(project_value_importance_votes.c.value_id.in_(value_ids or [UUID(int=0)]))
        ).all()
        if value_ids
        else []
    )

    votes_by_value: dict[UUID, list[tuple[UUID, int]]] = {}
    for value_id, voter_id, importance in importance_rows:
        votes_by_value.setdefault(value_id, []).append((voter_id, int(importance)))

    importance_scores_by_value_id: dict[UUID, float] = {}
    phase_one_values = []
    for value_id, label, value_author_id in value_rows:
        votes = votes_by_value.get(value_id, [])
        vote_count = len(votes)
        avg = (sum(score for _, score in votes) / vote_count) if vote_count > 0 else 0.0
        if avg >= 7:
            importance_label = "high"
        elif avg >= 4:
            importance_label = "medium"
        else:
            importance_label = "low"
        active_importance_vote = 0
        if current_user_id is not None:
            for voter_id, score in votes:
                if voter_id == current_user_id:
                    active_importance_vote = score
                    break
        importance_scores_by_value_id[value_id] = round(avg, 2)
        phase_one_values.append(
            {
                "id": str(value_id),
                "label": label,
                "authorUsername": usernames.get(value_author_id, {}).get("username", "unknown"),
                "voteCount": vote_count,
                "importanceScore": round(avg, 2),
                "importanceLabel": importance_label,
                "activeImportanceVote": active_importance_vote,
            }
        )

    viewer_signal = None
    if current_user_id is not None:
        viewer_signal_row = db.execute(
            select(project_signals.c.signal_type).where(
                project_signals.c.project_id == project_id,
                project_signals.c.user_id == current_user_id,
            )
        ).first()
        if viewer_signal_row is not None:
            viewer_signal = viewer_signal_row[0]

    required_demand = required_votes(vote_context_population)
    signal_summary = build_proposal_signal_summary(
        signal_counts,
        required_demand=required_demand,
        uses_platform_vote_context=uses_platform_vote_context,
        viewer_signal=viewer_signal,
        vote_context_label=vote_context_label,
        vote_context_population=vote_context_population,
    )

    plans_payload = load_project_plans(
        db,
        project_id=project_id,
        vote_context_population=vote_context_population,
        current_user_id=current_user_id,
        value_rows=value_rows,
        importance_scores_by_value_id=importance_scores_by_value_id,
        usernames=usernames,
        signal_counts=signal_counts,
    )
    phase_two_plans = plans_payload["phase_two_plans"]
    phase_three_plans = plans_payload["phase_three_plans"]
    phase_two_winning = plans_payload["phase_two_winning"]
    phase_three_winning = plans_payload["phase_three_winning"]

    def _selectable_plan_phases_from_winning_plan() -> list[dict[str, str]]:
        winning_id = phase_three_winning or phase_two_winning
        if not winning_id:
            return []
        all_plans = [*phase_two_plans, *phase_three_plans]
        winning_plan = next((plan for plan in all_plans if plan["id"] == winning_id), None)
        if winning_plan is None:
            return []
        return [
            {"id": phase["id"], "label": phase["title"]}
            for phase in winning_plan.get("planPhases", [])
        ]

    activities_rows = (
        db.execute(
            select(project_activities)
            .where(project_activities.c.project_id == project_id)
            .order_by(project_activities.c.scheduled_at.desc())
        )
        .mappings()
        .all()
    )
    activity_ids = [row_v["id"] for row_v in activities_rows]
    role_rows = (
        db.execute(
            select(project_activity_roles).where(
                project_activity_roles.c.activity_id.in_(activity_ids or [UUID(int=0)])
            )
        )
        .mappings()
        .all()
        if activity_ids
        else []
    )

    role_ids = [role["id"] for role in role_rows]
    assignment_rows = (
        db.execute(
            select(
                project_activity_assignments.c.role_id, project_activity_assignments.c.user_id
            ).where(project_activity_assignments.c.role_id.in_(role_ids or [UUID(int=0)]))
        ).all()
        if role_ids
        else []
    )

    assignments_by_role: dict[UUID, list[UUID]] = {}
    for role_id, user_id in assignment_rows:
        assignments_by_role.setdefault(role_id, []).append(user_id)

    roles_by_activity: dict[UUID, list[Mapping[str, object]]] = {}
    for role in role_rows:
        roles_by_activity.setdefault(role["activity_id"], []).append(role)

    live_activities: list[dict[str, object]] = []
    ended_activity_payloads: list[dict[str, object]] = []
    activity_rows_by_id: dict[UUID, Mapping[str, object]] = {}
    assignments_by_activity: dict[UUID, set[UUID]] = {}
    now = utc_now()
    for activity in activities_rows:
        activity_roles = []
        minimum_participants = 0
        maximum_participants: int | None = 0
        committed_users: set[UUID] = set()
        viewer_assigned_label = None

        for role in roles_by_activity.get(activity["id"], []):
            assigned_users = assignments_by_role.get(role["id"], [])
            committed_users.update(assigned_users)
            is_viewer_assigned = current_user_id is not None and current_user_id in assigned_users
            if is_viewer_assigned:
                viewer_assigned_label = role["label"]

            minimum_participants += int(role["required_count"] or 0)
            if role["maximum_count"] is None:
                maximum_participants = None
            elif maximum_participants is not None:
                maximum_participants += int(role["maximum_count"])

            activity_roles.append(
                {
                    "label": role["label"],
                    "filledCount": len(assigned_users),
                    "requiredCount": int(role["required_count"] or 0),
                    "maximumCount": role["maximum_count"],
                    "isViewerAssigned": is_viewer_assigned,
                    "assignees": [
                        {
                            "username": usernames.get(user_id, {}).get("username", "unknown"),
                            "profileImageUrl": usernames.get(user_id, {}).get("profileImageUrl"),
                        }
                        for user_id in assigned_users
                    ],
                }
            )

        activity_payload = {
            "id": str(activity["id"]),
            "title": activity["title"],
            "authorUsername": usernames.get(activity["author_id"], {}).get("username", "unknown"),
            "scheduledAt": _iso(activity["scheduled_at"]),
            "startAt": _iso(activity["scheduled_at"]),
            "endAt": _iso(activity["ends_at"]),
            "isOnline": bool(activity.get("is_online", False)),
            "locationLabel": activity["location_label"],
            "minimumParticipants": minimum_participants,
            "maximumParticipants": maximum_participants,
            "committedCount": len(committed_users),
            "viewerAssignedRoleLabel": viewer_assigned_label,
            "linkedPlanPhaseLabel": activity["linked_plan_phase_id"],
            "statusTone": activity_status_tone(len(committed_users), minimum_participants),
            "roles": activity_roles,
            "note": activity["note"],
            "isActive": not is_activity_ended(activity["ends_at"], now),
            "rolesLocked": is_activity_ended(activity["ends_at"], now),
        }
        activity_rows_by_id[activity["id"]] = activity
        assignments_by_activity[activity["id"]] = committed_users
        if is_activity_ended(activity["ends_at"], now):
            ended_activity_payloads.append(activity_payload)
        else:
            live_activities.append(activity_payload)

    ended_activity_ids = [UUID(activity["id"]) for activity in ended_activity_payloads]
    project_ratings_by_activity = load_project_ratings_by_activity(
        db, ended_activity_ids, usernames
    )
    history, history_needs_commit = build_project_history_items(
        db,
        project_id=project_id,
        ended_activities=ended_activity_payloads,
        activity_rows_by_id=activity_rows_by_id,
        assignments_by_activity=assignments_by_activity,
        usernames=usernames,
        current_user_id=current_user_id,
        ratings_by_activity=project_ratings_by_activity,
    )
    if history_needs_commit:
        db.commit()

    updates_rows = (
        db.execute(
            select(project_updates)
            .where(project_updates.c.project_id == project_id)
            .order_by(project_updates.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    updates = [
        {
            "id": str(item["id"]),
            "title": item["title"],
            "body": item["body"],
            "authorUsername": usernames.get(item["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
        }
        for item in updates_rows
    ]

    def _build_request_list(
        request_table, vote_table, body_keys: list[str]
    ) -> list[dict[str, object]]:
        rows = (
            db.execute(
                select(request_table)
                .where(request_table.c.project_id == project_id)
                .order_by(request_table.c.created_at.desc())
            )
            .mappings()
            .all()
        )
        open_rows = [req for req in rows if req["status"] == "open"]
        request_ids = [req["id"] for req in open_rows]
        votes_by_request: dict[object, list[tuple[object, object]]] = {}
        if request_ids:
            for request_id, vote, voter_id in db.execute(
                select(vote_table.c.request_id, vote_table.c.vote, vote_table.c.voter_id).where(
                    vote_table.c.request_id.in_(request_ids)
                )
            ).all():
                votes_by_request.setdefault(request_id, []).append((vote, voter_id))
        out = []
        for req in open_rows:
            vote_rows = votes_by_request.get(req["id"], [])
            summary, passes, can_still = _vote_summary(
                vote_rows, vote_context_population, current_user_id
            )
            payload = {
                "id": str(req["id"]),
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
            for key in body_keys:
                payload[key] = req[key.lower()] if key.lower() in req else req.get(key)
            out.append(payload)
        return out

    update_requests = _build_request_list(
        project_update_requests, project_update_request_votes, ["body"]
    )
    edit_requests = _build_request_list(
        project_edit_requests, project_edit_request_votes, ["title", "description"]
    )

    phase_change_rows = (
        db.execute(
            select(project_phase_change_requests)
            .where(project_phase_change_requests.c.project_id == project_id)
            .order_by(project_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    phase_title_map = {item[0]: item[3] for item in PROJECT_PHASES}
    phase_change_requests = []
    open_phase_rows = [req for req in phase_change_rows if req["status"] == "open"]
    phase_votes_by_request: dict[object, list[tuple[object, object]]] = {}
    if open_phase_rows:
        for request_id, vote, voter_id in db.execute(
            select(
                project_phase_change_votes.c.request_id,
                project_phase_change_votes.c.vote,
                project_phase_change_votes.c.voter_id,
            ).where(
                project_phase_change_votes.c.request_id.in_([req["id"] for req in open_phase_rows])
            )
        ).all():
            phase_votes_by_request.setdefault(request_id, []).append((vote, voter_id))
    for req in open_phase_rows:
        vote_rows = phase_votes_by_request.get(req["id"], [])
        summary, passes, can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        conversion_target = None
        if req["conversion_target_mode"]:
            mode = str(req["conversion_target_mode"])
            subtype = (
                str(req["conversion_target_subtype"]) if req["conversion_target_subtype"] else None
            )
            conversion_target = {
                "projectMode": mode,
                "projectSubtype": subtype,
                "projectModeLabel": mode.replace("-", " ").title(),
                "projectSubtypeLabel": (subtype or "n/a").replace("-", " ").title(),
                "entryPhaseId": "phase-1",
                "entryPhaseLabel": phase_title_map.get("phase-1", "Proposal"),
            }
        phase_change_requests.append(
            {
                "id": str(req["id"]),
                "targetPhaseId": req["target_phase_id"],
                "targetPhaseLabel": phase_title_map.get(
                    req["target_phase_id"], req["target_phase_id"]
                ),
                "reason": req["reason"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "kind": req["change_kind"],
                "closeOutcome": req["close_outcome"],
                "conversionTarget": conversion_target,
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    revert_rows = (
        db.execute(
            select(project_revert_history)
            .where(project_revert_history.c.project_id == project_id)
            .order_by(project_revert_history.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    revert_history = [
        {
            "id": str(item["id"]),
            "targetPhaseId": item["target_phase_id"],
            "reason": item["reason"],
            "authorUsername": usernames.get(item["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
        }
        for item in revert_rows
    ]

    service_settings = (
        db.execute(
            select(project_service_request_settings).where(
                project_service_request_settings.c.project_id == project_id
            )
        )
        .mappings()
        .first()
    )
    if service_settings is None:
        service_settings_payload = {
            "enabled": row["project_mode"] == "personal-service",
            "requestMode": "both",
            "allowOffScheduleRequests": row["project_mode"] == "personal-service",
            "summary": "",
        }
    else:
        service_settings_payload = {
            "enabled": bool(service_settings["enabled"]),
            "requestMode": service_settings["request_mode"],
            "allowOffScheduleRequests": bool(service_settings["allow_off_schedule_requests"]),
            "summary": service_settings["summary"],
        }

    service_requests_rows = (
        db.execute(
            select(project_service_requests)
            .where(project_service_requests.c.project_id == project_id)
            .order_by(project_service_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    service_requests = [
        {
            "id": str(item["id"]),
            "title": item["title"],
            "body": item["body"],
            "requesterUsername": usernames.get(item["requester_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
            "status": item["status"],
            "scheduledAt": _iso(item["scheduled_at"]) if item["scheduled_at"] else None,
            "endsAt": _iso(item["ends_at"]) if item["ends_at"] else None,
            "linkedActivityId": str(item["linked_activity_id"])
            if item["linked_activity_id"]
            else None,
        }
        for item in service_requests_rows
    ]

    settings_change_rows = (
        db.execute(
            select(project_service_request_setting_changes)
            .where(
                project_service_request_setting_changes.c.project_id == project_id,
                project_service_request_setting_changes.c.status == "open",
            )
            .order_by(project_service_request_setting_changes.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    settings_change_requests = []
    for req in settings_change_rows:
        vote_rows = db.execute(
            select(
                project_service_request_setting_change_votes.c.vote,
                project_service_request_setting_change_votes.c.voter_id,
            ).where(project_service_request_setting_change_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        settings_change_requests.append(
            {
                "id": str(req["id"]),
                "reason": req["reason"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "proposedSettings": {
                    "enabled": bool(req["enabled"]),
                    "requestMode": req["request_mode"],
                    "allowOffScheduleRequests": bool(req["allow_off_schedule_requests"]),
                    "summary": service_settings_payload["summary"],
                },
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    phase_order = {phase_id: order for phase_id, order, _, _, _ in PROJECT_PHASES}
    current_order = phase_order.get(row["current_phase_id"], 1)
    from app.services.projects_phases import display_stage_label, next_phase_id_for_project

    next_phase_id = next_phase_id_for_project(
        str(row["project_mode"]),
        str(row["project_subtype"]) if row["project_subtype"] else None,
        str(row["current_phase_id"]),
    )
    next_phase_label = (
        display_stage_label(
            str(row["project_mode"]),
            str(row["project_subtype"]) if row["project_subtype"] else None,
            next_phase_id,
        )
        if next_phase_id
        else None
    )

    software_governance = None
    effective_subtype = _resolve_effective_project_subtype(db, project_id, row["project_subtype"])
    if effective_subtype and row["project_subtype"] != effective_subtype:
        db.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(project_subtype=effective_subtype)
        )
        db.commit()
    if effective_subtype == "software":
        software_governance = get_project_software_governance(
            db=db, project_slug=row["slug"], current_user_id=current_user_id
        )

    is_personal_service = row["project_mode"] == "personal-service"
    viewer_can_request_update = viewer_is_author if is_personal_service else viewer_is_member
    viewer_can_vote_on_update_requests = (
        False if is_personal_service else viewer_can_cast_governance
    )
    viewer_can_request_edit = viewer_is_author if is_personal_service else viewer_is_member
    viewer_can_vote_on_edit_requests = False if is_personal_service else viewer_can_cast_governance
    viewer_can_create_activities = viewer_is_author if is_personal_service else viewer_is_member
    viewer_can_submit_requests = (
        (current_user_id is not None and not viewer_is_author)
        if is_personal_service
        else viewer_is_member
    )
    viewer_can_request_settings_changes = (
        viewer_is_author if is_personal_service else viewer_is_member
    )
    viewer_can_vote_on_settings_changes = (
        False if is_personal_service else viewer_can_cast_governance
    )
    viewer_can_request_phase_changes = False if is_personal_service else viewer_is_member
    viewer_can_vote_on_phase_changes = False if is_personal_service else viewer_can_cast_governance
    viewer_can_propose_links = False if is_personal_service else viewer_is_member
    phase_one_member_flags = (
        {
            "viewerCanSignalDemand": False,
            "viewerCanSignalOpposition": False,
            "viewerCanAddValue": False,
            "viewerCanVoteOnValues": False,
        }
        if is_personal_service
        else {
            "viewerCanSignalDemand": current_user_id is not None,
            "viewerCanSignalOpposition": current_user_id is not None,
            "viewerCanAddValue": viewer_is_member,
            "viewerCanVoteOnValues": viewer_is_member,
        }
    )
    phase_plan_member_flags = (
        {"viewerCanSubmitPlans": False, "viewerCanVoteOnPlans": False}
        if is_personal_service
        else {
            "viewerCanSubmitPlans": viewer_is_member,
            "viewerCanVoteOnPlans": viewer_can_cast_governance,
        }
    )

    request_system = {
        "enabled": bool(service_settings_payload["enabled"]),
        "requestCount": len(service_requests),
        "requests": service_requests,
        "viewerCanSubmitRequests": viewer_can_submit_requests,
        "viewerCanReviewRequests": viewer_can_review_requests,
        "viewerCanRequestSettingsChanges": viewer_can_request_settings_changes,
        "viewerCanVoteOnSettingsChanges": viewer_can_vote_on_settings_changes,
        "requiresSchedule": service_settings_payload["requestMode"] == "calendar",
        "settings": service_settings_payload,
        "settingsChangeRequests": settings_change_requests,
    }

    lifecycle = {
        "projectMode": row["project_mode"],
        "currentSubtype": effective_subtype,
        "currentSubtypeLabel": _subtype_label(effective_subtype) if effective_subtype else None,
        "usesPlatformLifecycle": row["project_mode"] != "personal-service",
        "supportsDemandSignals": not is_personal_service,
        "supportsPlanning": row["project_mode"] != "personal-service",
        "currentPhaseId": row["current_phase_id"],
        "quorumThresholdPercent": (
            required_votes(vote_context_population) / vote_context_population * 100.0
        )
        if vote_context_population > 0
        else 0.0,
        "quorumVotesRequired": required_votes(vote_context_population),
        "voteContextLabel": vote_context_label,
        "voteContextPopulation": vote_context_population,
        "notes": [],
        "phases": _visible_lifecycle_phases(
            str(row["project_mode"]),
            str(row["project_subtype"]) if row["project_subtype"] else None,
            str(row["current_phase_id"]),
        ),
        "viewerCanRequestPhaseChanges": viewer_can_request_phase_changes,
        "viewerCanVoteOnPhaseChanges": viewer_can_vote_on_phase_changes,
        "phaseChangeRequests": phase_change_requests,
        "viewerCanAdvancePhase": viewer_is_manager,
        "nextPhaseId": next_phase_id,
        "nextPhaseLabel": next_phase_label,
        "viewerCanRevertPhase": viewer_is_manager,
        "revertablePhaseIds": [
            phase_id
            for phase_id, order, _, _, _ in PROJECT_PHASES
            if order < current_order and order <= 3
        ],
        "revertHistory": revert_history,
        "requestSystem": request_system,
        "personalService": {
            "availabilitySummary": "",
            "travelRadiusLabel": "",
            "usesCalendar": service_settings_payload["requestMode"] in {"calendar", "both"},
            "requestMode": service_settings_payload["requestMode"],
        }
        if row["project_mode"] == "personal-service"
        else None,
        "phaseOne": {
            "values": phase_one_values,
            **phase_one_member_flags,
            "viewerHasDemandSignal": viewer_signal == "demand",
            "viewerHasOppositionSignal": viewer_signal == "opposition",
            "signalSummary": signal_summary,
        },
        "phaseTwo": {
            "plans": phase_two_plans,
            "winningPlanId": phase_two_winning,
            **phase_plan_member_flags,
            "availableAssetManagementServices": [],
        },
        "phaseThree": {
            "plans": phase_three_plans,
            "winningPlanId": phase_three_winning,
            **phase_plan_member_flags,
            "requestSystemEnabled": bool(service_settings_payload["enabled"]),
        },
        "phaseFour": None,
        "phaseFive": {
            "activities": live_activities,
            "history": history,
            "viewerCanCreateActivities": viewer_can_create_activities,
            "selectablePlanPhases": _selectable_plan_phases_from_winning_plan(),
            "softwareGovernance": software_governance,
        },
    }

    report_row = (
        db.execute(
            select(
                reports.c.id,
                reports.c.resolution,
                projects.c.moderation_state,
                projects.c.moderation_reason,
                projects.c.created_at,
                projects.c.vote_count,
                projects.c.comment_count,
            )
            .select_from(
                projects.outerjoin(
                    reports,
                    (reports.c.target_type == "project") & (reports.c.target_id == projects.c.id),
                )
            )
            .where(projects.c.id == project_id)
        )
        .mappings()
        .first()
    )
    report = None
    is_removed = False
    moderation_state = "visible"
    moderation_reason = None
    if report_row is not None:
        moderation_state = str(report_row.get("moderation_state") or "visible")
        moderation_reason = report_row.get("moderation_reason")
        is_removed = moderation_state == "removed" or report_row.get("resolution") == "removed"
        if report_row.get("id") is not None and report_row.get("resolution") in {
            "open",
            "under_review",
            "hidden",
        }:
            from app.services.moderation.serialize import load_active_report

            report = load_active_report(
                db,
                target_type="project",
                target_id=project_id,
                current_user_id=current_user_id,
                created_at=report_row.get("created_at"),
            )

    if is_removed:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    discussion_rows = (
        db.execute(
            select(
                comments.c.id,
                comments.c.author_id,
                comments.c.body,
                comments.c.created_at,
                comments.c.vote_count,
                comments.c.moderation_state,
                comments.c.moderation_reason,
            )
            .where(
                comments.c.subject_type == "project",
                comments.c.subject_id == project_id,
                comments.c.parent_id.is_(None),
            )
            .order_by(comments.c.created_at.asc())
        ).all()
        if include_tab_payloads
        else []
    )
    discussion_author_ids = {
        author_id for _, author_id, _, _, _, _, _ in discussion_rows if author_id
    }
    missing_discussion_author_ids = discussion_author_ids - set(usernames.keys())
    if missing_discussion_author_ids:
        usernames.update(_username_lookup(db, missing_discussion_author_ids))
    discussion_comment_ids = [comment_id for comment_id, _, _, _, _, _, _ in discussion_rows]
    discussion_active_votes: dict[UUID, int] = {}
    if current_user_id is not None and discussion_comment_ids:
        dv_rows = db.execute(
            select(content_votes.c.target_id, content_votes.c.direction).where(
                content_votes.c.target_type == "comment",
                content_votes.c.target_id.in_(discussion_comment_ids),
                content_votes.c.voter_id == current_user_id,
            )
        ).all()
        discussion_active_votes = {target_id: int(direction) for target_id, direction in dv_rows}
    from app.services.moderation.serialize import (
        load_active_reports_for_targets,
        moderation_body_for_comment,
    )

    discussion_reports = load_active_reports_for_targets(
        db,
        target_type="comment",
        target_ids=discussion_comment_ids,
        current_user_id=current_user_id,
    )
    discussion = [
        {
            "id": str(comment_id),
            "authorUsername": usernames.get(author_id, {}).get("username", "unknown"),
            "body": moderation_body_for_comment(
                body=body,
                moderation_state=str(moderation_state or "visible"),
                moderation_reason=str(moderation_reason) if moderation_reason else None,
            ),
            "createdAt": _iso(created_at),
            "voteCount": int(vote_count or 0),
            "activeVote": discussion_active_votes.get(comment_id, 0),
            "moderationState": str(moderation_state or "visible"),
            "moderationReason": moderation_reason,
            "report": discussion_reports.get(comment_id),
            "replies": [],
        }
        for (
            comment_id,
            author_id,
            body,
            created_at,
            vote_count,
            moderation_state,
            moderation_reason,
        ) in discussion_rows
    ]

    if include_tab_payloads:
        links_frame = _build_enriched_project_links_frame(
            db,
            row,
            current_user_id=current_user_id,
            vote_context_population=vote_context_population,
            usernames=usernames,
            member_count=member_count,
            viewer_can_cast_governance=viewer_can_cast_governance,
            viewer_can_propose_links=viewer_can_propose_links,
            phase_title_map=phase_title_map,
            phase_change_rows=phase_change_rows,
        )
    else:
        links_frame = empty_links_frame("project", str(row["slug"]))

    return {
        "id": str(project_id),
        "slug": row["slug"],
        "createdAt": _iso(row["created_at"]),
        "title": row["title"],
        "authorUsername": author_username,
        "projectMode": row["project_mode"],
        "projectSubtype": row["project_subtype"],
        "description": row["description"],
        "channelTags": channel_tags,
        "communityTags": community_tags,
        "stage": display_stage_label(
            str(row["project_mode"]),
            str(row["project_subtype"]) if row["project_subtype"] else None,
            str(row["current_phase_id"]),
        ),
        "locationLabel": row["location_label"],
        "locationId": str(row["location_id"]) if row.get("location_id") else None,
        "voteCount": int(row["vote_count"] or 0),
        "activeVote": active_vote,
        "signalCount": signal_counts["total"],
        "commentCount": int(row["comment_count"] or 0),
        "memberCount": member_count,
        "lastActivityAt": _iso(row["last_activity_at"]),
        "lifecycle": lifecycle,
        "updates": updates,
        "updateRequests": update_requests,
        "viewerCanRequestUpdate": viewer_can_request_update,
        "viewerCanVoteOnUpdateRequests": viewer_can_vote_on_update_requests,
        "editRequests": edit_requests,
        "viewerCanRequestEdit": viewer_can_request_edit,
        "viewerCanVoteOnEditRequests": viewer_can_vote_on_edit_requests,
        "linksFrame": links_frame,
        "inventoryFrame": None,
        "history": (
            _build_project_history(db, project_id, current_user_id, vote_context_population)
            if include_tab_payloads
            else []
        ),
        "members": members,
        "viewerIsMember": viewer_is_member,
        "viewerCanToggleMembership": current_user_id is not None,
        "viewerCanShare": viewer_is_member,
        "shareContacts": share_contacts,
        "report": report,
        "isRemovedByReport": is_removed,
        "moderationState": moderation_state,
        "moderationReason": moderation_reason,
        "isUnderReview": moderation_state == "under_review",
        "discussionNote": "",
        "discussion": discussion,
    }


async def get_project_history(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    project_id = row["id"]
    assert_can_view_entity(db, current_user_id, "project", project_id)
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(row["is_platform_tagged"]),
    )
    return {
        "history": _build_project_history(db, project_id, current_user_id, vote_context_population)
    }


def _build_enriched_project_links_frame(
    db: Session,
    row: Mapping[str, object],
    *,
    current_user_id: UUID | None,
    vote_context_population: int,
    usernames: Mapping[object, Mapping[str, object]],
    member_count: int,
    viewer_can_cast_governance: bool,
    viewer_can_propose_links: bool,
    phase_title_map: Mapping[str, str],
    phase_change_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    project_id = row["id"]
    links_frame = build_links_frame(
        db,
        owner_kind="project",
        owner_slug=str(row["slug"]),
        current_user_id=current_user_id,
    )

    link_rows = (
        db.execute(select(project_links).where(project_links.c.source_project_id == project_id))
        .mappings()
        .all()
    )
    target_ids = {item["target_project_id"] for item in link_rows}
    target_projects = {}
    if target_ids:
        for target in (
            db.execute(
                select(projects.c.id, projects.c.slug, projects.c.title).where(
                    projects.c.id.in_(list(target_ids))
                )
            )
            .mappings()
            .all()
        ):
            target_projects[target["id"]] = target

    auto_links = []
    conversion_auto_links = []
    for item in link_rows:
        target = target_projects.get(item["target_project_id"])
        frame_item = {
            "id": str(item["id"]),
            "title": target["title"] if target else item["relationship_label"],
            "relationshipLabel": item["relationship_label"],
            "summary": item["summary"],
            "href": f"/projects/{target['slug']}" if target else None,
            "publicItem": None,
        }
        if item["link_kind"] == CONVERSION_LINK_KIND:
            conversion_auto_links.append(frame_item)
        else:
            auto_links.append(frame_item)

    conversion_row = (
        db.execute(
            select(project_conversions)
            .where(
                (project_conversions.c.predecessor_project_id == project_id)
                | (project_conversions.c.successor_project_id == project_id)
            )
            .limit(1)
        )
        .mappings()
        .first()
    )
    conversion_lineage = None
    if conversion_row is not None:
        pred = (
            db.execute(
                select(projects.c.id, projects.c.slug, projects.c.title).where(
                    projects.c.id == conversion_row["predecessor_project_id"]
                )
            )
            .mappings()
            .one()
        )
        succ = (
            db.execute(
                select(projects.c.id, projects.c.slug, projects.c.title).where(
                    projects.c.id == conversion_row["successor_project_id"]
                )
            )
            .mappings()
            .one()
        )
        conversion_lineage = {
            "title": "Conversion lineage",
            "statusLabel": "Permanent",
            "summary": conversion_row["summary"],
            "permanenceNote": conversion_row["permanence_note"] or PERMANENCE_NOTE,
            "inventoryNote": conversion_row["inventory_note"] or INVENTORY_NOTE,
            "predecessor": {
                "id": str(pred["id"]),
                "title": pred["title"],
                "relationshipLabel": CONVERSION_FROM_LABEL,
                "summary": conversion_row["summary"],
                "href": f"/projects/{pred['slug']}",
                "publicItem": None,
            },
            "successor": {
                "id": str(succ["id"]),
                "title": succ["title"],
                "relationshipLabel": CONVERSION_TO_LABEL,
                "summary": conversion_row["summary"],
                "href": f"/projects/{succ['slug']}",
                "publicItem": None,
            },
        }

    conversion_workflow = []
    for req in phase_change_rows:
        if req["status"] != "open" or req["close_outcome"] != "convert":
            continue
        vote_rows = db.execute(
            select(project_phase_change_votes.c.vote, project_phase_change_votes.c.voter_id).where(
                project_phase_change_votes.c.request_id == req["id"]
            )
        ).all()
        summary, _passes, _can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        mode = str(req["conversion_target_mode"] or "collective-service")
        subtype = (
            str(req["conversion_target_subtype"]) if req["conversion_target_subtype"] else None
        )
        conversion_workflow.append(
            {
                "id": str(req["id"]),
                "title": req["conversion_successor_title"] or "Convert project",
                "statusLabel": "Open conversion vote",
                "requestedByUsername": usernames.get(req["author_id"], {}).get(
                    "username", "unknown"
                ),
                "createdAtLabel": _iso(req["created_at"]),
                "outcomeLabel": "Pending electorate approval",
                "summary": req["reason"],
                "inventoryNote": INVENTORY_NOTE,
                "canVote": viewer_can_cast_governance,
                "voteSummary": summary,
                "approvalThresholdPercent": 66,
                "target": {
                    "projectMode": mode,
                    "projectSubtype": subtype,
                    "projectModeLabel": mode.replace("-", " ").title(),
                    "projectSubtypeLabel": (subtype or "n/a").replace("-", " ").title(),
                    "entryPhaseId": "phase-1",
                    "entryPhaseLabel": phase_title_map.get("phase-1", "Proposal"),
                },
                "predecessor": {
                    "id": str(project_id),
                    "title": row["title"],
                    "relationshipLabel": CONVERSION_FROM_LABEL,
                    "summary": req["reason"],
                    "href": f"/projects/{row['slug']}",
                    "publicItem": None,
                },
                "successor": None,
            }
        )

    link_request_rows = (
        db.execute(
            select(project_link_requests)
            .where(project_link_requests.c.source_project_id == project_id)
            .order_by(project_link_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    request_ids = [req["id"] for req in link_request_rows]
    votes_by_request: dict[object, list[object]] = {}
    if request_ids:
        for request_id, vote in db.execute(
            select(
                project_link_request_votes.c.request_id, project_link_request_votes.c.vote
            ).where(
                project_link_request_votes.c.request_id.in_(request_ids),
                project_link_request_votes.c.vote_scope == "source",
            )
        ).all():
            votes_by_request.setdefault(request_id, []).append(vote)
    manual_link_requests = []
    for req in link_request_rows:
        this_votes = votes_by_request.get(req["id"], [])
        yes = sum(1 for vote in this_votes if vote == "yes")
        no = sum(1 for vote in this_votes if vote == "no")
        manual_link_requests.append(
            {
                "id": str(req["id"]),
                "title": req["relationship_label"],
                "relationshipLabel": req["relationship_label"],
                "summary": req["summary"],
                "statusLabel": req["status"],
                "proposedByUsername": usernames.get(req["proposed_by"], {}).get(
                    "username", "unknown"
                ),
                "createdAtLabel": _iso(req["created_at"]),
                "targetProjectHref": None,
                "thisProjectVote": {
                    "projectTitle": row["title"],
                    "yesCount": yes,
                    "noCount": no,
                    "memberCount": member_count,
                    "approvalsRequired": required_votes(vote_context_population),
                    "approvalsRemaining": max(0, required_votes(vote_context_population) - yes),
                    "approvalPercent": (yes / (yes + no) * 100.0) if (yes + no) else 0.0,
                    "statusLabel": req["status"],
                    "resultNote": "",
                    "viewerCanVote": viewer_can_cast_governance,
                    "viewerVote": None,
                },
                "otherProjectVote": {
                    "projectTitle": "Linked project",
                    "yesCount": 0,
                    "noCount": 0,
                    "memberCount": 0,
                    "approvalsRequired": 0,
                    "approvalsRemaining": 0,
                    "approvalPercent": 0,
                    "statusLabel": "pending",
                    "resultNote": "",
                    "viewerCanVote": False,
                    "viewerVote": None,
                },
            }
        )

    linkable_rows = db.execute(
        select(projects.c.slug, projects.c.title)
        .where(projects.c.id != project_id)
        .order_by(projects.c.title.asc())
        .limit(20)
    ).all()
    linkable_projects = [
        {"slug": s, "title": t, "href": f"/projects/{s}"} for s, t in linkable_rows
    ]

    links_frame["conversionNote"] = (
        "Conversion lineage is permanent. Manual links stay visible alongside it."
        if conversion_lineage
        else ""
    )
    links_frame["conversionWorkflow"] = conversion_workflow
    links_frame["conversionLineage"] = conversion_lineage
    links_frame["projectSlug"] = row["slug"]
    links_frame["autoLinks"] = conversion_auto_links + auto_links
    links_frame["manualLinks"] = links_frame["activeLinks"]
    links_frame["manualLinkRequests"] = manual_link_requests
    links_frame["linkableProjects"] = linkable_projects
    links_frame["viewerCanProposeLinks"] = viewer_can_propose_links
    links_frame["requestFrames"] = [
        {"id": "borrowing", "title": "Borrowing", "body": ""},
        {"id": "delivery", "title": "Delivery", "body": ""},
        {"id": "asset-use", "title": "Asset use", "body": ""},
    ]
    links_frame["placeholderSections"] = []
    return links_frame


async def get_project_links(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    project_id = row["id"]
    assert_can_view_entity(db, current_user_id, "project", project_id)
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(row["is_platform_tagged"]),
    )
    membership_rows = db.execute(
        select(project_memberships.c.user_id).where(project_memberships.c.project_id == project_id)
    ).all()
    member_ids = {user_id for (user_id,) in membership_rows}
    usernames = _username_lookup(
        db, member_ids | ({row["author_id"]} if row["author_id"] else set())
    )
    member_count = int(row["member_count"] or 0)
    viewer_is_member = current_user_id is not None and current_user_id in member_ids
    is_personal_service = str(row["project_mode"]) == "personal-service"
    viewer_can_cast_governance = False if is_personal_service else viewer_is_member
    viewer_can_propose_links = False if is_personal_service else viewer_is_member
    phase_title_map = {item[0]: item[3] for item in PROJECT_PHASES}
    phase_change_rows = (
        db.execute(
            select(project_phase_change_requests)
            .where(project_phase_change_requests.c.project_id == project_id)
            .order_by(project_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    return {
        "linksFrame": _build_enriched_project_links_frame(
            db,
            row,
            current_user_id=current_user_id,
            vote_context_population=vote_context_population,
            usernames=usernames,
            member_count=member_count,
            viewer_can_cast_governance=viewer_can_cast_governance,
            viewer_can_propose_links=viewer_can_propose_links,
            phase_title_map=phase_title_map,
            phase_change_rows=phase_change_rows,
        )
    }
