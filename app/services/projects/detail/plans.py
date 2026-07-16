"""Plan hydration helpers for project detail."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    project_plan_criterion_ratings,
    project_plan_votes,
    project_plans,
)
from app.services.plan_criteria import (
    assessment_criteria_for_plan,
    serialize_plan_criterion_assessments,
)
from app.services.projects.helpers import _iso, _plan_leader_status, _vote_summary
from app.services.projects_plans import _plan_subtype_from_payload, _subtype_label


def load_project_plans(
    db: Session,
    *,
    project_id: UUID,
    vote_context_population: int,
    current_user_id: UUID | None,
    value_rows: list,
    importance_scores_by_value_id: dict,
    usernames: dict,
    signal_counts: dict[str, int],
) -> dict[str, object]:
    plan_rows = (
        db.execute(
            select(project_plans)
            .where(project_plans.c.project_id == project_id)
            .order_by(project_plans.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    passing_by_phase_kind: dict[str, list[tuple[str, float]]] = {}
    for plan in plan_rows:
        plan_vote_rows = db.execute(
            select(project_plan_votes.c.vote, project_plan_votes.c.voter_id).where(
                project_plan_votes.c.plan_id == plan["id"]
            )
        ).all()
        overall_summary, passes, _ = _vote_summary(
            plan_vote_rows, vote_context_population, current_user_id
        )
        plan_id_str = str(plan["id"])
        if passes:
            passing_by_phase_kind.setdefault(plan["phase_kind"], []).append(
                (plan_id_str, overall_summary["approvalPercent"])
            )

    phase_two_plans = []
    phase_three_plans = []
    phase_two_leading: list[str] = []
    phase_three_leading: list[str] = []

    for plan in plan_rows:
        plan_vote_rows = db.execute(
            select(project_plan_votes.c.vote, project_plan_votes.c.voter_id).where(
                project_plan_votes.c.plan_id == plan["id"]
            )
        ).all()
        overall_summary, passes, _ = _vote_summary(
            plan_vote_rows, vote_context_population, current_user_id
        )
        leader_status = _plan_leader_status(
            is_leading=bool(plan["is_leading"]),
            passes=passes,
            approval_percent=overall_summary["approvalPercent"],
            passing_plans=passing_by_phase_kind.get(plan["phase_kind"], []),
        )

        value_assessments = []
        criterion_rating_rows = db.execute(
            select(
                project_plan_criterion_ratings.c.criterion_id,
                project_plan_criterion_ratings.c.rating,
                project_plan_criterion_ratings.c.voter_id,
            ).where(project_plan_criterion_ratings.c.plan_id == plan["id"])
        ).all()
        ratings_by_criterion: dict[str, list[tuple[int, UUID]]] = {}
        for criterion_id, rating, voter_id in criterion_rating_rows:
            ratings_by_criterion.setdefault(criterion_id, []).append((rating, voter_id))

        prominent_value_tuples = [
            (value_id, value_label)
            for value_id, value_label, _ in value_rows
            if importance_scores_by_value_id.get(value_id, 0) >= 5
        ]
        criterion_assessments = serialize_plan_criterion_assessments(
            assessment_criteria_for_plan(
                plan_kind=str(plan["phase_kind"]),
                prominent_values=prominent_value_tuples,
                project_subtype=plan["project_subtype"],
            ),
            ratings_by_criterion,
            current_user_id,
        )

        plan_payload = dict(plan["plan_payload"] or {})
        value_consideration_notes = dict(plan_payload.get("valueConsiderationNotes") or {})
        plan_phases = [
            {
                "id": str(item.get("id") or f"phase-{idx + 1}"),
                "title": str(item.get("title") or f"Phase {idx + 1}"),
                "details": str(item.get("details") or ""),
                "materialsLabel": str(item.get("materialsLabel") or ""),
                "costLabel": str(item.get("costLabel") or ""),
            }
            for idx, item in enumerate(list(plan_payload.get("planPhases") or []))
        ]

        base_plan = {
            "id": str(plan["id"]),
            "title": plan["title"],
            "authorUsername": usernames.get(plan["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(plan["created_at"]),
            "description": plan["description"],
            "repositoryUrl": plan["repository_url"],
            "demandSignalSnapshot": signal_counts["demand"],
            "demandConsiderationNote": plan["demand_consideration_note"] or "",
            "valueConsiderationNotes": value_consideration_notes,
            "totalCostLabel": plan["total_cost_label"] or "",
            "planPhases": plan_phases,
            "valueAssessments": value_assessments,
            "criterionAssessments": criterion_assessments,
            "overallApproval": overall_summary,
            "isLeading": bool(plan["is_leading"]),
            "leaderStatus": leader_status,
        }

        if plan["phase_kind"] in {"production", "organisation"}:
            resolved_plan_subtype = (
                plan["project_subtype"] or _plan_subtype_from_payload(plan_payload) or "standard"
            )
            item = {
                **base_plan,
                "projectSubtype": resolved_plan_subtype,
                "projectSubtypeLabel": _subtype_label(resolved_plan_subtype),
                "outputSummary": str(plan_payload.get("outputSummary") or ""),
                "materialsSummary": str(plan_payload.get("materialsSummary") or ""),
                "acquisitionsSummary": str(plan_payload.get("acquisitionsSummary") or ""),
                "acquisitionBundles": list(plan_payload.get("acquisitionBundles") or []),
                "purchaseRows": list(plan_payload.get("purchaseRows") or []),
                "viewerCanEdit": current_user_id is not None
                and plan["author_id"] == current_user_id,
            }
            phase_two_plans.append(item)
            if plan["is_leading"]:
                phase_two_leading.append(str(plan["id"]))

        if plan["phase_kind"] in {"distribution", "access"}:
            item = {
                **base_plan,
                "distributionSummary": str(plan_payload.get("distributionSummary") or ""),
                "accessSummary": str(plan_payload.get("accessSummary") or ""),
                "reserveSummary": str(plan_payload.get("reserveSummary") or ""),
                "requestSystemEnabled": bool(plan_payload.get("requestSystemEnabled") or False),
                "requestMode": str(plan_payload.get("requestMode") or "both"),
                "allowOffScheduleRequests": bool(
                    plan_payload.get("allowOffScheduleRequests") or False
                ),
            }
            phase_three_plans.append(item)
            if plan["is_leading"]:
                phase_three_leading.append(str(plan["id"]))

    phase_two_winning = phase_two_leading[0] if len(phase_two_leading) == 1 else None
    phase_three_winning = phase_three_leading[0] if len(phase_three_leading) == 1 else None
    return {
        "phase_two_plans": phase_two_plans,
        "phase_three_plans": phase_three_plans,
        "phase_two_winning": phase_two_winning,
        "phase_three_winning": phase_three_winning,
    }
