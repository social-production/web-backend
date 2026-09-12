from __future__ import annotations

from uuid import UUID

SHARED_RUBRIC: list[dict[str, str]] = [
    {
        "id": "rubric:description-clarity",
        "label": "Does the plan clearly explain what will happen and why?",
    },
    {
        "id": "rubric:achievability",
        "label": "Does this plan seem realistically achievable?",
    },
]

EVENT_RUBRIC: list[dict[str, str]] = [
    {"id": "rubric:timing-suitable", "label": "Is the timing and schedule realistic?"},
    {"id": "rubric:location-appropriate", "label": "Is the location appropriate and accessible?"},
]

PROJECT_PRODUCTION_RUBRIC: list[dict[str, str]] = [
    {
        "id": "rubric:production-approach",
        "label": "Is the proposed production approach appropriate?",
    },
]

PROJECT_SOFTWARE_RUBRIC: list[dict[str, str]] = [
    {
        "id": "rubric:repository-clear",
        "label": "Is the repository/setup clear enough?",
    },
]

PROJECT_DISTRIBUTION_RUBRIC: list[dict[str, str]] = [
    {
        "id": "rubric:access-approach",
        "label": "Is the access and request approach appropriate?",
    },
]

VALID_PLAN_RATINGS = {1, 2, 3, 4, 5}


def value_criterion_id(value_id: UUID | str) -> str:
    return f"value:{value_id}"


def parse_value_criterion_id(criterion_id: str) -> UUID | None:
    if not criterion_id.startswith("value:"):
        return None
    try:
        return UUID(criterion_id.split(":", 1)[1])
    except ValueError:
        return None


def assessment_criteria_for_plan(
    *,
    plan_kind: str,
    prominent_values: list[tuple[UUID, str]],
    project_subtype: str | None = None,
) -> list[dict[str, object]]:
    criteria: list[dict[str, object]] = [
        {"criterionId": item["id"], "kind": "rubric", "label": item["label"]}
        for item in SHARED_RUBRIC
    ]

    if plan_kind == "event":
        criteria.extend(
            {"criterionId": item["id"], "kind": "rubric", "label": item["label"]}
            for item in EVENT_RUBRIC
        )
    elif plan_kind in {"production", "organisation"}:
        criteria.extend(
            {"criterionId": item["id"], "kind": "rubric", "label": item["label"]}
            for item in PROJECT_PRODUCTION_RUBRIC
        )
        if project_subtype == "software":
            criteria.extend(
                {"criterionId": item["id"], "kind": "rubric", "label": item["label"]}
                for item in PROJECT_SOFTWARE_RUBRIC
            )
    elif plan_kind == "distribution":
        criteria.extend(
            {"criterionId": item["id"], "kind": "rubric", "label": item["label"]}
            for item in PROJECT_DISTRIBUTION_RUBRIC
        )

    for value_id, value_label in prominent_values:
        criteria.append(
            {
                "criterionId": value_criterion_id(value_id),
                "kind": "value",
                "label": f'How well does this plan satisfy "{value_label}"?',
                "valueId": str(value_id),
            }
        )

    return criteria


def criterion_rating_summary(
    rating_rows: list[tuple[int, UUID]],
    current_user_id: UUID | None,
) -> dict[str, object]:
    distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    active_rating = None
    total = 0

    for rating, voter_id in rating_rows:
        if rating not in VALID_PLAN_RATINGS:
            continue
        distribution[rating] += 1
        total += rating
        if current_user_id is not None and voter_id == current_user_id:
            active_rating = rating

    rating_count = sum(distribution.values())
    average_rating = round(total / rating_count, 2) if rating_count > 0 else 0.0

    return {
        "activeRating": active_rating,
        "averageRating": average_rating,
        "ratingCount": rating_count,
        "ratingDistribution": distribution,
    }


def serialize_plan_criterion_assessments(
    criteria: list[dict[str, object]],
    rating_rows_by_criterion: dict[str, list[tuple[int, UUID]]],
    current_user_id: UUID | None,
) -> list[dict[str, object]]:
    assessments: list[dict[str, object]] = []

    for criterion in criteria:
        criterion_id = str(criterion["criterionId"])
        summary = criterion_rating_summary(
            rating_rows_by_criterion.get(criterion_id, []),
            current_user_id,
        )
        assessments.append({**criterion, **summary})

    return assessments


def plan_average_rating(
    rating_rows_by_criterion: dict[str, list[tuple[int, UUID]]],
) -> float:
    """Mean of per-criterion averages that have at least one rating."""
    averages: list[float] = []
    for rows in rating_rows_by_criterion.values():
        summary = criterion_rating_summary(rows, None)
        if int(summary["ratingCount"]) > 0:
            averages.append(float(summary["averageRating"]))
    if not averages:
        return 0.0
    return round(sum(averages) / len(averages), 4)


def pick_leading_plan_id(
    candidates: list[tuple[UUID, float, float]],
) -> UUID | None:
    """Pick a unique leader by approval ratio, then average rating. Ties return None."""
    if not candidates:
        return None

    max_ratio = max(ratio for _, ratio, _ in candidates)
    top_ratio = [item for item in candidates if item[1] == max_ratio]
    if len(top_ratio) == 1:
        return top_ratio[0][0]

    max_avg = max(avg for _, _, avg in top_ratio)
    top_avg = [item for item in top_ratio if item[2] == max_avg]
    if len(top_avg) == 1:
        return top_avg[0][0]

    return None
