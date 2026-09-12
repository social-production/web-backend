from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    event_activities,
    event_activity_assignments,
    event_activity_roles,
    event_edit_request_votes,
    event_edit_requests,
    event_editors,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_criterion_ratings,
    event_plan_votes,
    event_plans,
    event_signals,
    event_tags,
    event_update_request_votes,
    event_update_requests,
    event_updates,
    event_value_importance_votes,
    event_values,
    events,
)
from app.services.access_control import (
    COMMUNITY_SCOPE_KIND,
    assert_can_view_entity,
    is_scope_member,
)
from app.services.activity_history import (
    build_event_history_items,
    is_activity_ended,
    load_event_ratings_by_activity,
    utc_now,
)
from app.services.content import activity_status_tone
from app.services.detail_links import (
    build_link_decision_history_entries,
    build_links_frame,
    empty_links_frame,
)
from app.services.events.helpers import (
    _can_propose_event_activity,
    _event_lifecycle_phases,
    _get_event_by_slug_row,
    _get_signal_counts,
    _get_signal_counts_db,
    _iso,
    _plan_leader_status,
    _username_lookup,
    _vote_summary,
)
from app.services.locations import get_location, is_map_eligible, serialize_location
from app.services.people_suggestions import get_ranked_people_suggestions
from app.services.plan_criteria import (
    assessment_criteria_for_plan,
    plan_average_rating,
    serialize_plan_criterion_assessments,
)
from app.services.signal_gates import build_proposal_signal_summary
from app.utils.votes import is_platform_event, required_votes, resolve_event_vote_population

EVENT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
_PLACEHOLDER_SCHEDULE_LABELS = frozenset({"tbd", "not specified", "to be determined"})
EVENT_PHASES = (
    ("proposal", 1, "P1", "Proposal", "Collect demand and define event values."),
    ("event-plan", 2, "P2", "Event Plan", "Propose and approve event plans."),
    ("activity", 3, "P3", "Activity", "Run event activities."),
    ("closed", 4, "P4", "Closed", "Event is closed."),
)


async def get_event_detail(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
    cache: Redis | None = None,
    include_tab_payloads: bool = False,
) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    event_id = row["id"]
    member_count = int(row["member_count"] or 0)
    uses_platform_vote_context = is_platform_event(db, event_id)
    vote_context_population = resolve_event_vote_population(db, event_id)
    vote_context_label = (
        "Weekly active platform users"
        if uses_platform_vote_context
        else "Weekly active event members"
    )

    if cache is not None:
        try:
            signal_counts = await _get_signal_counts(db, cache, event_id)
        except Exception:
            signal_counts = _get_signal_counts_db(db, event_id)
    else:
        signal_counts = _get_signal_counts_db(db, event_id)

    membership_rows = db.execute(
        select(event_memberships.c.user_id, event_memberships.c.role).where(
            event_memberships.c.event_id == event_id
        )
    ).all()
    member_ids = {user_id for user_id, _ in membership_rows}
    usernames = _username_lookup(
        db, member_ids | ({row["created_by"]} if row["created_by"] else set())
    )

    audience = str(row["audience"] or "public")
    governance = str(row["governance"] or "collaborative")
    is_organizer_controlled = governance == "organizer_controlled"
    home_community_id = row["home_community_id"]

    viewer_is_member = current_user_id is not None and current_user_id in member_ids
    viewer_is_home_community_member = bool(
        home_community_id is not None
        and current_user_id is not None
        and is_scope_member(db, COMMUNITY_SCOPE_KIND, home_community_id, current_user_id)
    )
    if row["is_private"] and not viewer_is_member and not viewer_is_home_community_member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    assert_can_view_entity(db, current_user_id, "event", event_id)

    editor_rows = db.execute(
        select(event_editors.c.user_id).where(event_editors.c.event_id == event_id)
    ).all()
    editor_ids = {user_id for (user_id,) in editor_rows}

    creator_username = usernames.get(row["created_by"], {}).get("username", "unknown")

    channel_tag_rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(event_tags.join(channels, channels.c.id == event_tags.c.channel_id))
        .where(event_tags.c.event_id == event_id, event_tags.c.tag_kind == "channel")
    ).all()
    channel_tags = [
        {"slug": slug_v, "label": name, "kind": "channel"} for slug_v, name in channel_tag_rows
    ]

    community_tag_rows = db.execute(
        select(communities.c.slug, communities.c.name)
        .select_from(event_tags.join(communities, communities.c.id == event_tags.c.community_id))
        .where(event_tags.c.event_id == event_id, event_tags.c.tag_kind == "community")
    ).all()
    community_tags = [
        {"slug": slug_v, "label": name, "kind": "community"} for slug_v, name in community_tag_rows
    ]

    home_community = None
    if home_community_id is not None:
        home_community_row = db.execute(
            select(communities.c.slug, communities.c.name).where(
                communities.c.id == home_community_id
            )
        ).first()
        if home_community_row is not None:
            home_community = {"slug": home_community_row[0], "name": home_community_row[1]}

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "event",
                content_votes.c.target_id == event_id,
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    attendees = [usernames.get(user_id, {}).get("username", "unknown") for user_id in member_ids]

    organizer_ids: set[UUID] = set(editor_ids)
    organizer_ids.add(row["created_by"])

    def _member_payload(user_id: UUID) -> dict[str, object]:
        return {
            "id": str(user_id),
            "username": usernames.get(user_id, {}).get("username", "unknown"),
            "bio": usernames.get(user_id, {}).get("bio", ""),
        }

    # Organizers = creator (always) + promoted editors; creator listed first.
    ordered_organizer_ids: list[UUID] = [row["created_by"]]
    for user_id in sorted(
        editor_ids, key=lambda value: usernames.get(value, {}).get("username", "")
    ):
        if user_id != row["created_by"] and user_id not in ordered_organizer_ids:
            ordered_organizer_ids.append(user_id)

    event_editors_payload = [_member_payload(user_id) for user_id in ordered_organizer_ids]

    members = [
        _member_payload(user_id) for user_id, _ in membership_rows if user_id not in organizer_ids
    ]

    available_editor_invitees = [
        _member_payload(user_id) for user_id, _ in membership_rows if user_id not in organizer_ids
    ]

    share_contacts: list[dict[str, object]] = []
    if current_user_id is not None:
        ranked = get_ranked_people_suggestions(
            db,
            current_user_id,
            limit=24,
            exclude_ids=set(member_ids),
        )
        share_contacts = [
            {
                "id": str(item["id"]),
                "username": str(item["username"]),
                "bio": str(item.get("bio") or ""),
            }
            for item in ranked
        ]

    viewer_signal = None
    if current_user_id is not None:
        viewer_signal = db.execute(
            select(event_signals.c.signal_type).where(
                event_signals.c.event_id == event_id,
                event_signals.c.user_id == current_user_id,
            )
        ).scalar_one_or_none()

    required_demand = required_votes(vote_context_population)
    signal_summary = build_proposal_signal_summary(
        signal_counts,
        required_demand=required_demand,
        uses_platform_vote_context=uses_platform_vote_context,
        viewer_signal=viewer_signal,
        vote_context_label=vote_context_label,
        vote_context_population=vote_context_population,
    )

    value_rows = db.execute(
        select(event_values.c.id, event_values.c.label, event_values.c.author_id)
        .where(event_values.c.event_id == event_id)
        .order_by(event_values.c.created_at.asc())
    ).all()
    value_ids = [value_id for value_id, _, _ in value_rows]
    importance_rows = (
        db.execute(
            select(
                event_value_importance_votes.c.value_id,
                event_value_importance_votes.c.voter_id,
                event_value_importance_votes.c.importance,
            ).where(event_value_importance_votes.c.value_id.in_(value_ids or [UUID(int=0)]))
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

    plan_rows = (
        db.execute(
            select(event_plans)
            .where(event_plans.c.event_id == event_id)
            .order_by(event_plans.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    passing_plans: list[tuple[str, float, float]] = []
    average_by_plan_id: dict[str, float] = {}
    ratings_by_plan_id: dict[str, dict[str, list[tuple[int, UUID]]]] = {}
    for plan in plan_rows:
        plan_id_str = str(plan["id"])
        criterion_rating_rows = db.execute(
            select(
                event_plan_criterion_ratings.c.criterion_id,
                event_plan_criterion_ratings.c.rating,
                event_plan_criterion_ratings.c.voter_id,
            ).where(event_plan_criterion_ratings.c.plan_id == plan["id"])
        ).all()
        ratings_by_criterion: dict[str, list[tuple[int, UUID]]] = {}
        for criterion_id, rating, voter_id in criterion_rating_rows:
            ratings_by_criterion.setdefault(str(criterion_id), []).append((rating, voter_id))
        ratings_by_plan_id[plan_id_str] = ratings_by_criterion
        average_by_plan_id[plan_id_str] = plan_average_rating(ratings_by_criterion)

        plan_vote_rows = db.execute(
            select(event_plan_votes.c.vote, event_plan_votes.c.voter_id).where(
                event_plan_votes.c.plan_id == plan["id"]
            )
        ).all()
        overall_summary, passes, _ = _vote_summary(
            plan_vote_rows, vote_context_population, current_user_id
        )
        if passes:
            passing_plans.append(
                (plan_id_str, overall_summary["approvalPercent"], average_by_plan_id[plan_id_str])
            )

    event_plans_payload = []
    leading_plan_ids: list[str] = []
    for plan in plan_rows:
        plan_id_str = str(plan["id"])
        plan_vote_rows = db.execute(
            select(event_plan_votes.c.vote, event_plan_votes.c.voter_id).where(
                event_plan_votes.c.plan_id == plan["id"]
            )
        ).all()
        overall_summary, passes, _ = _vote_summary(
            plan_vote_rows, vote_context_population, current_user_id
        )
        leader_status = _plan_leader_status(
            is_leading=bool(plan["is_leading"]),
            passes=passes,
            approval_percent=overall_summary["approvalPercent"],
            average_rating=average_by_plan_id.get(plan_id_str, 0.0),
            passing_plans=passing_plans,
        )

        value_assessments = []
        ratings_by_criterion = ratings_by_plan_id.get(plan_id_str, {})

        prominent_value_tuples = [
            (value_id, value_label)
            for value_id, value_label, _ in value_rows
            if importance_scores_by_value_id.get(value_id, 0) >= 5
        ]
        criterion_assessments = serialize_plan_criterion_assessments(
            assessment_criteria_for_plan(
                plan_kind="event",
                prominent_values=prominent_value_tuples,
            ),
            ratings_by_criterion,
            current_user_id,
        )

        schedule_payload = dict(plan["schedule_payload"] or {})
        schedule_mode = str(schedule_payload.get("mode") or "any-day")
        start_date = schedule_payload.get("startDate") or schedule_payload.get("start_date")
        end_date = schedule_payload.get("endDate") or schedule_payload.get("end_date")
        start_time_label = schedule_payload.get("startTimeLabel") or schedule_payload.get(
            "start_time_label"
        )
        finish_time_label = schedule_payload.get("finishTimeLabel") or schedule_payload.get(
            "finish_time_label"
        )
        schedule = {
            "mode": schedule_mode,
            "startDate": start_date,
            "endDate": end_date,
            "startTimeLabel": start_time_label,
            "finishTimeLabel": finish_time_label,
            "label": schedule_payload.get("label") or schedule_mode.replace("-", " ").title(),
        }

        plan_payload = dict(plan["plan_payload"] or {})
        value_consideration_notes = dict(plan_payload.get("valueConsiderationNotes") or {})
        plan_phases = [
            {
                "id": str(item.get("id") or f"phase-{idx + 1}"),
                "title": str(item.get("title") or f"Phase {idx + 1}"),
                "details": str(item.get("details") or ""),
            }
            for idx, item in enumerate(list(plan_payload.get("planPhases") or []))
        ]

        event_plans_payload.append(
            {
                "id": str(plan["id"]),
                "title": plan["title"],
                "authorUsername": usernames.get(plan["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(plan["created_at"]),
                "description": plan["description"],
                "demandSignalSnapshot": signal_counts["demand"],
                "demandConsiderationNote": plan["demand_consideration_note"] or "",
                "valueConsiderationNotes": value_consideration_notes,
                "locationLabel": plan["location_label"],
                "locationId": str(plan["location_id"]) if plan.get("location_id") else None,
                "schedule": schedule,
                "planPhases": plan_phases,
                "valueAssessments": value_assessments,
                "criterionAssessments": criterion_assessments,
                "averageRating": average_by_plan_id.get(plan_id_str, 0.0),
                "overallApproval": overall_summary,
                "isLeading": bool(plan["is_leading"]),
                "leaderStatus": leader_status,
            }
        )
        if plan["is_leading"]:
            leading_plan_ids.append(str(plan["id"]))

    winning_plan_id = leading_plan_ids[0] if len(leading_plan_ids) == 1 else None

    activities_rows = (
        db.execute(
            select(event_activities)
            .where(event_activities.c.event_id == event_id)
            .order_by(event_activities.c.scheduled_at.desc())
        )
        .mappings()
        .all()
    )
    activity_ids = [row_v["id"] for row_v in activities_rows]
    role_rows = (
        db.execute(
            select(event_activity_roles).where(
                event_activity_roles.c.activity_id.in_(activity_ids or [UUID(int=0)])
            )
        )
        .mappings()
        .all()
        if activity_ids
        else []
    )

    role_ids = [role["id"] for role in role_rows]
    suggested_ids = {
        role["suggested_user_id"] for role in role_rows if role.get("suggested_user_id")
    }
    if suggested_ids:
        usernames.update(_username_lookup(db, suggested_ids))
    assignment_rows = (
        db.execute(
            select(
                event_activity_assignments.c.role_id, event_activity_assignments.c.user_id
            ).where(event_activity_assignments.c.role_id.in_(role_ids or [UUID(int=0)]))
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
                    "id": str(role["id"]),
                    "label": role["label"],
                    "filledCount": len(assigned_users),
                    "requiredCount": int(role["required_count"] or 0),
                    "maximumCount": role["maximum_count"],
                    "isViewerAssigned": is_viewer_assigned,
                    "suggestedUser": (
                        {
                            "id": str(role["suggested_user_id"]),
                            "username": usernames.get(role["suggested_user_id"], {}).get(
                                "username", "unknown"
                            ),
                        }
                        if role.get("suggested_user_id")
                        and role.get("suggestion_status") != "declined"
                        else None
                    ),
                    "suggestionStatus": role.get("suggestion_status"),
                    "isViewerSuggested": current_user_id is not None
                    and role.get("suggested_user_id") == current_user_id,
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
            "isActive": not is_activity_ended(activity["ends_at"], now)
            and len(committed_users) >= minimum_participants,
            "rolesLocked": is_activity_ended(activity["ends_at"], now),
        }
        assignments_by_activity[activity["id"]] = committed_users
        if is_activity_ended(activity["ends_at"], now):
            ended_activity_payloads.append(activity_payload)
        else:
            live_activities.append(activity_payload)

    ended_activity_ids = [UUID(activity["id"]) for activity in ended_activity_payloads]
    event_ratings_by_activity = load_event_ratings_by_activity(db, ended_activity_ids, usernames)
    activity_history, history_needs_commit = build_event_history_items(
        db,
        event_id=event_id,
        ended_activities=ended_activity_payloads,
        assignments_by_activity=assignments_by_activity,
        current_user_id=current_user_id,
        ratings_by_activity=event_ratings_by_activity,
    )
    if history_needs_commit:
        db.commit()

    selectable_plan_phases = []
    winning_plan = None
    if winning_plan_id is not None:
        winning_plan = next(
            (plan for plan in event_plans_payload if plan["id"] == winning_plan_id), None
        )
        if winning_plan is not None:
            selectable_plan_phases = [
                {"id": phase["id"], "label": phase["title"]} for phase in winning_plan["planPhases"]
            ]

    can_propose_activities = _can_propose_event_activity(
        winning_plan.get("schedule") if winning_plan is not None else None
    )

    updates_rows = (
        db.execute(
            select(event_updates)
            .where(event_updates.c.event_id == event_id)
            .order_by(event_updates.c.created_at.desc())
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

    update_request_rows = (
        db.execute(
            select(event_update_requests)
            .where(event_update_requests.c.event_id == event_id)
            .order_by(event_update_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    update_requests = []
    history_entries: list[tuple[object, dict[str, object]]] = []
    visible_update_rows = [
        req for req in update_request_rows if include_tab_payloads or req["status"] == "open"
    ]
    update_votes = _votes_by_request(
        db, event_update_request_votes, [req["id"] for req in visible_update_rows]
    )
    for req in visible_update_rows:
        vote_rows = update_votes.get(req["id"], [])
        summary, passes, can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        if include_tab_payloads:
            history_entries.append(
                (
                    req["created_at"],
                    {
                        "id": str(req["id"]),
                        "entityKind": "event",
                        "kind": "event-update",
                        "kindLabel": "Update decision",
                        "createdAt": _iso(req["created_at"]),
                        "authorUsername": usernames.get(req["author_id"], {}).get(
                            "username", "unknown"
                        ),
                        "status": req["status"],
                        "approvalThresholdPercent": 66,
                        "voteSummary": summary,
                        "passesApprovalThreshold": passes,
                        "canStillPass": can_still,
                        "canVote": (
                            viewer_is_member
                            and req["status"] == "open"
                            and not is_organizer_controlled
                        ),
                        "payload": {
                            "type": "update",
                            "body": req["body"],
                            "appliedUpdateId": None,
                        },
                    },
                )
            )
        if req["status"] != "open":
            continue
        update_requests.append(
            {
                "id": str(req["id"]),
                "body": req["body"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    edit_request_rows = (
        db.execute(
            select(event_edit_requests)
            .where(event_edit_requests.c.event_id == event_id)
            .order_by(event_edit_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    edit_requests = []
    visible_edit_rows = [
        req for req in edit_request_rows if include_tab_payloads or req["status"] == "open"
    ]
    edit_votes = _votes_by_request(
        db, event_edit_request_votes, [req["id"] for req in visible_edit_rows]
    )
    for req in visible_edit_rows:
        vote_rows = edit_votes.get(req["id"], [])
        summary, passes, can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        if include_tab_payloads:
            history_entries.append(
                (
                    req["created_at"],
                    {
                        "id": str(req["id"]),
                        "entityKind": "event",
                        "kind": "event-edit",
                        "kindLabel": "Edit decision",
                        "createdAt": _iso(req["created_at"]),
                        "authorUsername": usernames.get(req["author_id"], {}).get(
                            "username", "unknown"
                        ),
                        "status": req["status"],
                        "approvalThresholdPercent": 66,
                        "voteSummary": summary,
                        "passesApprovalThreshold": passes,
                        "canStillPass": can_still,
                        "canVote": (
                            viewer_is_member
                            and req["status"] == "open"
                            and not is_organizer_controlled
                        ),
                        "payload": {
                            "type": "edit",
                            "changes": [
                                {
                                    "label": "Title",
                                    "before": str(row["title"]),
                                    "after": str(req["title"]),
                                },
                                {
                                    "label": "Description",
                                    "before": str(row["description"]),
                                    "after": str(req["description"]),
                                },
                            ],
                        },
                    },
                )
            )
        if req["status"] != "open":
            continue
        edit_requests.append(
            {
                "id": str(req["id"]),
                "title": req["title"],
                "description": req["description"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    phase_change_rows = (
        db.execute(
            select(event_phase_change_requests)
            .where(event_phase_change_requests.c.event_id == event_id)
            .order_by(event_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    phase_title_map = {item[0]: item[3] for item in EVENT_PHASES}
    phase_change_requests = []
    visible_phase_rows = [
        req for req in phase_change_rows if include_tab_payloads or req["status"] == "open"
    ]
    phase_votes = _votes_by_request(
        db, event_phase_change_votes, [req["id"] for req in visible_phase_rows]
    )
    for req in visible_phase_rows:
        vote_rows = phase_votes.get(req["id"], [])
        summary, passes, can_still = _vote_summary(
            vote_rows, vote_context_population, current_user_id
        )
        if include_tab_payloads:
            history_entries.append(
                (
                    req["created_at"],
                    {
                        "id": str(req["id"]),
                        "entityKind": "event",
                        "kind": "event-phase-change",
                        "kindLabel": "Phase decision",
                        "createdAt": _iso(req["created_at"]),
                        "authorUsername": usernames.get(req["author_id"], {}).get(
                            "username", "unknown"
                        ),
                        "status": req["status"],
                        "approvalThresholdPercent": 66,
                        "voteSummary": summary,
                        "passesApprovalThreshold": passes,
                        "canStillPass": can_still,
                        "canVote": (
                            viewer_is_member
                            and req["status"] == "open"
                            and not is_organizer_controlled
                        ),
                        "payload": {
                            "type": "phase-change",
                            "changeKind": req["change_kind"],
                            "fromPhaseId": req["from_phase_id"],
                            "fromPhaseLabel": phase_title_map.get(
                                req["from_phase_id"], req["from_phase_id"]
                            ),
                            "toPhaseId": req["target_phase_id"],
                            "toPhaseLabel": phase_title_map.get(
                                req["target_phase_id"], req["target_phase_id"]
                            ),
                            "reason": req["reason"],
                        },
                    },
                )
            )
        if req["status"] != "open":
            continue
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
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    phase_order = {phase_id: order for phase_id, order, _, _, _ in EVENT_PHASES}
    current_order = phase_order.get(row["current_phase_id"], 1)
    previous_phase = next((p for p in EVENT_PHASES if p[1] == current_order - 1), None)
    next_phase = next((p for p in EVENT_PHASES if p[1] == current_order + 1), None)
    viewer_is_organizer = bool(
        current_user_id is not None
        and (current_user_id == row["created_by"] or current_user_id in editor_ids)
    )
    in_proposal = row["current_phase_id"] == "proposal"
    in_event_plan = row["current_phase_id"] == "event-plan"
    viewer_can_cast_governance = (
        current_user_id is not None if uses_platform_vote_context else viewer_is_member
    )

    lifecycle = {
        "currentPhaseId": row["current_phase_id"],
        "quorumThresholdPercent": (
            required_votes(vote_context_population) / vote_context_population * 100.0
        )
        if vote_context_population > 0
        else 0.0,
        "quorumVotesRequired": required_votes(vote_context_population),
        "voteContextLabel": vote_context_label,
        "voteContextPopulation": vote_context_population,
        "phases": _event_lifecycle_phases(row["current_phase_id"]),
        "phaseOne": {
            "values": phase_one_values,
            "viewerCanSignalDemand": current_user_id is not None and in_proposal,
            "viewerHasDemandSignal": viewer_signal == "demand",
            "viewerCanSignalOpposition": current_user_id is not None and in_proposal,
            "viewerHasOppositionSignal": viewer_signal == "opposition",
            "signalSummary": signal_summary,
            "viewerCanAddValue": viewer_is_member and in_proposal,
            "viewerCanVoteOnValues": viewer_is_member and in_proposal,
        },
        "phaseTwo": {
            "plans": event_plans_payload,
            "winningPlanId": winning_plan_id,
            "viewerCanSubmitPlans": viewer_is_member and in_event_plan,
            "viewerCanVoteOnPlans": viewer_can_cast_governance and in_event_plan,
        },
        "activity": {
            "activities": live_activities,
            "history": activity_history,
            "viewerCanCreateActivities": viewer_is_member and can_propose_activities,
            "selectablePlanPhases": selectable_plan_phases,
        },
        "viewerCanRequestPhaseChanges": viewer_is_member,
        "viewerCanVoteOnPhaseChanges": viewer_can_cast_governance,
        "phaseChangeRequests": phase_change_requests,
        "revertablePhaseIds": [
            phase_id for phase_id, order, _, _, _ in EVENT_PHASES if order < current_order
        ],
        "previousPhaseId": previous_phase[0] if previous_phase else None,
        "previousPhaseLabel": previous_phase[3] if previous_phase else None,
        "nextPhaseId": next_phase[0] if next_phase else None,
        "nextPhaseLabel": next_phase[3] if next_phase else None,
    }

    event_moderation = (
        db.execute(
            select(
                events.c.moderation_state, events.c.moderation_reason, events.c.created_at
            ).where(events.c.id == event_id)
        )
        .mappings()
        .first()
    )
    moderation_state = str((event_moderation or {}).get("moderation_state") or "visible")
    moderation_reason = (event_moderation or {}).get("moderation_reason")
    if moderation_state == "removed":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    from app.services.moderation.serialize import load_active_report

    report = load_active_report(
        db,
        target_type="event",
        target_id=event_id,
        current_user_id=current_user_id,
        created_at=(event_moderation or {}).get("created_at"),
    )
    is_removed = False

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
                comments.c.subject_type == "event",
                comments.c.subject_id == event_id,
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

    viewer_is_organizer = bool(
        current_user_id is not None
        and (current_user_id == row["created_by"] or current_user_id in editor_ids)
    )
    viewer_has_edit_access = viewer_is_organizer
    viewer_can_edit_directly = viewer_is_organizer and is_organizer_controlled
    viewer_can_drive_lifecycle = (
        viewer_is_organizer if is_organizer_controlled else viewer_is_member
    )
    viewer_can_toggle_membership = bool(
        current_user_id is not None
        and (
            viewer_is_member
            or audience == "public"
            or (audience == "private_community" and viewer_is_home_community_member)
        )
    )
    invited_usernames = (
        [
            usernames.get(user_id, {}).get("username", "unknown")
            for user_id in member_ids
            if user_id != row["created_by"]
        ]
        if audience == "invite_only"
        else []
    )

    # Organizer-controlled private events skip collaborative proposal/plan participation.
    # Collaborative private events keep the same signal/value/plan rules as public events.
    if is_organizer_controlled:
        lifecycle["phaseOne"]["viewerCanSignalDemand"] = False
        lifecycle["phaseOne"]["viewerCanSignalOpposition"] = False
        lifecycle["phaseOne"]["viewerCanAddValue"] = False
        lifecycle["phaseOne"]["viewerCanVoteOnValues"] = False
        lifecycle["phaseTwo"]["viewerCanSubmitPlans"] = viewer_is_organizer and in_event_plan
        lifecycle["phaseTwo"]["viewerCanVoteOnPlans"] = False
        # Organizer-controlled events never reopen collaborative proposal participation.
        lifecycle["revertablePhaseIds"] = [
            phase_id for phase_id in lifecycle["revertablePhaseIds"] if phase_id != "proposal"
        ]
        if lifecycle.get("previousPhaseId") == "proposal":
            previous_usable = next(
                (phase_id for phase_id in lifecycle["revertablePhaseIds"]),
                None,
            )
            lifecycle["previousPhaseId"] = previous_usable
            lifecycle["previousPhaseLabel"] = (
                next(
                    (
                        label
                        for phase_id, _, _, label, _ in EVENT_PHASES
                        if phase_id == previous_usable
                    ),
                    None,
                )
                if previous_usable
                else None
            )
    else:
        lifecycle["phaseTwo"]["viewerCanSubmitPlans"] = viewer_can_drive_lifecycle and in_event_plan
        lifecycle["phaseTwo"]["viewerCanVoteOnPlans"] = viewer_can_cast_governance and in_event_plan
    lifecycle["viewerCanRequestPhaseChanges"] = viewer_can_drive_lifecycle
    lifecycle["viewerCanVoteOnPhaseChanges"] = (
        viewer_can_cast_governance and not is_organizer_controlled
    )
    lifecycle["activity"]["viewerCanCreateActivities"] = (
        viewer_is_organizer if is_organizer_controlled else viewer_is_member
    ) and can_propose_activities

    location_row = None
    location_id = row.get("location_id")
    if location_id is not None:
        location_row = get_location(db, location_id)
    viewer_can_see_private_location = viewer_is_member or viewer_is_home_community_member
    location_payload = serialize_location(
        location_row,
        viewer_authorized=viewer_can_see_private_location if bool(row["is_private"]) else True,
        is_private_entity=bool(row["is_private"]),
    )
    map_eligible = is_map_eligible(
        entity_kind="event",
        location=location_row if location_payload is not None else None,
    )

    links_frame = (
        build_links_frame(
            db,
            owner_kind="event",
            owner_slug=str(row["slug"]),
            current_user_id=current_user_id,
        )
        if include_tab_payloads
        else empty_links_frame("event", str(row["slug"]))
    )
    if include_tab_payloads:
        history_entries.extend(
            build_link_decision_history_entries(
                db,
                owner_kind="event",
                owner_id=event_id,
                owner_slug=str(row["slug"]),
                owner_title=str(row["title"]),
                current_user_id=current_user_id,
            )
        )

    return {
        "id": str(event_id),
        "slug": row["slug"],
        "createdAt": _iso(row["created_at"]),
        "title": row["title"],
        "description": row["description"],
        "isPrivate": bool(row["is_private"]),
        "audience": audience,
        "governance": governance,
        "homeCommunity": home_community,
        "scheduledAt": _iso(row["scheduled_at"]),
        "channelTags": channel_tags,
        "communityTags": community_tags,
        "createdByUsername": creator_username,
        "timeLabel": row["time_label"],
        "locationLabel": row["location_label"],
        "locationId": str(location_id) if location_id is not None else None,
        "location": location_payload,
        "mapEligible": map_eligible,
        "voteCount": int(row["vote_count"] or 0),
        "activeVote": active_vote,
        "commentCount": int(row["comment_count"] or 0),
        "memberCount": member_count,
        "lastActivityAt": _iso(row["last_activity_at"]),
        "signalSummary": signal_summary,
        "lifecycle": lifecycle,
        "attendanceNote": "",
        "agenda": [],
        "updates": updates,
        "updateRequests": update_requests,
        "viewerCanRequestUpdate": viewer_is_member,
        "viewerCanVoteOnUpdateRequests": viewer_can_cast_governance,
        "editRequests": edit_requests,
        "viewerCanRequestEdit": viewer_can_drive_lifecycle,
        "viewerCanVoteOnEditRequests": viewer_can_cast_governance and not is_organizer_controlled,
        "linksFrame": links_frame,
        "history": [
            entry for _, entry in sorted(history_entries, key=lambda item: item[0], reverse=True)
        ]
        if include_tab_payloads
        else [],
        "attendees": attendees,
        "invitedUsernames": invited_usernames,
        "eventEditors": event_editors_payload,
        "members": members,
        "viewerIsMember": viewer_is_member,
        "viewerIsOrganizer": viewer_is_organizer,
        "viewerCanEditDirectly": viewer_can_edit_directly,
        "viewerCanToggleMembership": viewer_can_toggle_membership,
        "viewerHasEventEditAccess": viewer_has_edit_access,
        "viewerCanManageEditors": bool(row["is_private"]) and viewer_is_organizer,
        "viewerCanShare": (viewer_is_organizer if bool(row["is_private"]) else viewer_is_member),
        "availableEditorInvitees": available_editor_invitees,
        "shareContacts": share_contacts,
        "report": report,
        "isRemovedByReport": is_removed,
        "moderationState": moderation_state,
        "moderationReason": moderation_reason,
        "isUnderReview": moderation_state == "under_review",
        "discussionNote": "",
        "discussion": discussion,
    }


def _votes_by_request(
    db: Session, vote_table, request_ids: list
) -> dict[object, list[tuple[object, object]]]:
    votes_by_request: dict[object, list[tuple[object, object]]] = {}
    if not request_ids:
        return votes_by_request
    for request_id, vote, voter_id in db.execute(
        select(vote_table.c.request_id, vote_table.c.vote, vote_table.c.voter_id).where(
            vote_table.c.request_id.in_(request_ids)
        )
    ).all():
        votes_by_request.setdefault(request_id, []).append((vote, voter_id))
    return votes_by_request


def _build_event_decision_history(
    db: Session,
    row: Mapping[str, object],
    *,
    current_user_id: UUID | None,
    vote_context_population: int,
    usernames: Mapping[object, Mapping[str, object]],
    viewer_is_member: bool,
    is_organizer_controlled: bool,
) -> list[tuple[object, dict[str, object]]]:
    event_id = row["id"]
    history_entries: list[tuple[object, dict[str, object]]] = []
    phase_title_map = {item[0]: item[3] for item in EVENT_PHASES}

    update_request_rows = (
        db.execute(
            select(event_update_requests)
            .where(event_update_requests.c.event_id == event_id)
            .order_by(event_update_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    update_votes = _votes_by_request(
        db, event_update_request_votes, [req["id"] for req in update_request_rows]
    )
    for req in update_request_rows:
        summary, passes, can_still = _vote_summary(
            update_votes.get(req["id"], []), vote_context_population, current_user_id
        )
        history_entries.append(
            (
                req["created_at"],
                {
                    "id": str(req["id"]),
                    "entityKind": "event",
                    "kind": "event-update",
                    "kindLabel": "Update decision",
                    "createdAt": _iso(req["created_at"]),
                    "authorUsername": usernames.get(req["author_id"], {}).get(
                        "username", "unknown"
                    ),
                    "status": req["status"],
                    "approvalThresholdPercent": 66,
                    "voteSummary": summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": (
                        viewer_is_member and req["status"] == "open" and not is_organizer_controlled
                    ),
                    "payload": {
                        "type": "update",
                        "body": req["body"],
                        "appliedUpdateId": None,
                    },
                },
            )
        )

    edit_request_rows = (
        db.execute(
            select(event_edit_requests)
            .where(event_edit_requests.c.event_id == event_id)
            .order_by(event_edit_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    edit_votes = _votes_by_request(
        db, event_edit_request_votes, [req["id"] for req in edit_request_rows]
    )
    for req in edit_request_rows:
        summary, passes, can_still = _vote_summary(
            edit_votes.get(req["id"], []), vote_context_population, current_user_id
        )
        history_entries.append(
            (
                req["created_at"],
                {
                    "id": str(req["id"]),
                    "entityKind": "event",
                    "kind": "event-edit",
                    "kindLabel": "Edit decision",
                    "createdAt": _iso(req["created_at"]),
                    "authorUsername": usernames.get(req["author_id"], {}).get(
                        "username", "unknown"
                    ),
                    "status": req["status"],
                    "approvalThresholdPercent": 66,
                    "voteSummary": summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": (
                        viewer_is_member and req["status"] == "open" and not is_organizer_controlled
                    ),
                    "payload": {
                        "type": "edit",
                        "changes": [
                            {
                                "label": "Title",
                                "before": str(row["title"]),
                                "after": str(req["title"]),
                            },
                            {
                                "label": "Description",
                                "before": str(row["description"]),
                                "after": str(req["description"]),
                            },
                        ],
                    },
                },
            )
        )

    phase_change_rows = (
        db.execute(
            select(event_phase_change_requests)
            .where(event_phase_change_requests.c.event_id == event_id)
            .order_by(event_phase_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    phase_votes = _votes_by_request(
        db, event_phase_change_votes, [req["id"] for req in phase_change_rows]
    )
    for req in phase_change_rows:
        summary, passes, can_still = _vote_summary(
            phase_votes.get(req["id"], []), vote_context_population, current_user_id
        )
        history_entries.append(
            (
                req["created_at"],
                {
                    "id": str(req["id"]),
                    "entityKind": "event",
                    "kind": "event-phase-change",
                    "kindLabel": "Phase decision",
                    "createdAt": _iso(req["created_at"]),
                    "authorUsername": usernames.get(req["author_id"], {}).get(
                        "username", "unknown"
                    ),
                    "status": req["status"],
                    "approvalThresholdPercent": 66,
                    "voteSummary": summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": (
                        viewer_is_member and req["status"] == "open" and not is_organizer_controlled
                    ),
                    "payload": {
                        "type": "phase-change",
                        "changeKind": req["change_kind"],
                        "fromPhaseId": req["from_phase_id"],
                        "fromPhaseLabel": phase_title_map.get(
                            req["from_phase_id"], req["from_phase_id"]
                        ),
                        "toPhaseId": req["target_phase_id"],
                        "toPhaseLabel": phase_title_map.get(
                            req["target_phase_id"], req["target_phase_id"]
                        ),
                        "reason": req["reason"],
                    },
                },
            )
        )

    history_entries.extend(
        build_link_decision_history_entries(
            db,
            owner_kind="event",
            owner_id=event_id,
            owner_slug=str(row["slug"]),
            owner_title=str(row["title"]),
            current_user_id=current_user_id,
        )
    )
    return history_entries


async def get_event_history(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    event_id = row["id"]
    assert_can_view_entity(db, current_user_id, "event", event_id)
    vote_context_population = resolve_event_vote_population(db, event_id)
    membership_rows = db.execute(
        select(event_memberships.c.user_id).where(event_memberships.c.event_id == event_id)
    ).all()
    member_ids = {user_id for (user_id,) in membership_rows}
    usernames = _username_lookup(
        db, member_ids | ({row["created_by"]} if row["created_by"] else set())
    )
    viewer_is_member = current_user_id is not None and current_user_id in member_ids
    is_organizer_controlled = str(row["governance"] or "collaborative") == "organizer_controlled"
    history_entries = _build_event_decision_history(
        db,
        row,
        current_user_id=current_user_id,
        vote_context_population=vote_context_population,
        usernames=usernames,
        viewer_is_member=viewer_is_member,
        is_organizer_controlled=is_organizer_controlled,
    )
    return {
        "history": [
            entry for _, entry in sorted(history_entries, key=lambda item: item[0], reverse=True)
        ]
    }


async def get_event_links(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    assert_can_view_entity(db, current_user_id, "event", row["id"])
    return {
        "linksFrame": build_links_frame(
            db,
            owner_kind="event",
            owner_slug=str(row["slug"]),
            current_user_id=current_user_id,
        )
    }
