from __future__ import annotations

from uuid import UUID

SHARED_RUBRIC: list[dict[str, str]] = [
    {"id": "rubric:title-clarity", "label": "Is the title clear and specific?"},
    {
        "id": "rubric:description-clarity",
        "label": "Does the description explain what will actually happen and why?",
    },
    {
        "id": "rubric:demand-response",
        "label": "Does this plan respond well to the current demand signal?",
    },
    {
        "id": "rubric:achievability",
        "label": "Does this plan seem realistically achievable?",
    },
    {
        "id": "rubric:stages-coherent",
        "label": "Are the stages coherent and in a sensible order?",
    },
]

EVENT_RUBRIC: list[dict[str, str]] = [
    {"id": "rubric:timing-suitable", "label": "Is the timing suitable?"},
    {"id": "rubric:duration-realistic", "label": "Is the duration/schedule realistic?"},
    {"id": "rubric:location-appropriate", "label": "Is the location appropriate and accessible?"},
]

PROJECT_PRODUCTION_RUBRIC: list[dict[str, str]] = [
    {
        "id": "rubric:production-approach",
        "label": "Is the proposed production approach appropriate?",
    },
    {
        "id": "rubric:materials-realistic",
        "label": "Are the listed materials/resources realistic?",
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
        "label": "Is the access/distribution approach appropriate?",
    },
    {"id": "rubric:request-settings", "label": "Are the request settings sensible?"},
    {
        "id": "rubric:off-schedule",
        "label": "Is off-schedule handling appropriate?",
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
