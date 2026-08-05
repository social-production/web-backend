from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, status

SIGNAL_DEMAND_RATIO_THRESHOLD_PERCENT = 66.0


def proposal_advancement_unlocked(
    signal_counts: Mapping[str, int],
    *,
    required_demand: int,
    uses_platform_vote_context: bool,
) -> bool:
    total = int(signal_counts.get("total") or 0)
    demand = int(signal_counts.get("demand") or 0)
    if total <= 0:
        return False

    ratio_met = (demand / total * 100.0) >= SIGNAL_DEMAND_RATIO_THRESHOLD_PERCENT
    if uses_platform_vote_context:
        return ratio_met and demand >= required_demand
    return ratio_met


def build_proposal_signal_summary(
    signal_counts: Mapping[str, int],
    *,
    required_demand: int,
    uses_platform_vote_context: bool,
    viewer_signal: str | None,
    vote_context_label: str,
    vote_context_population: int,
) -> dict[str, object]:
    total = int(signal_counts.get("total") or 0)
    demand = int(signal_counts.get("demand") or 0)
    opposition = int(signal_counts.get("opposition") or 0)
    ratio_percent = (demand / total * 100.0) if total > 0 else 0.0
    ratio_met = ratio_percent >= SIGNAL_DEMAND_RATIO_THRESHOLD_PERCENT if total > 0 else False
    demand_met = demand >= required_demand

    return {
        "demandCount": demand,
        "oppositionCount": opposition,
        "totalCount": total,
        "viewerSignal": viewer_signal,
        "signalRatioPercent": ratio_percent,
        "ratioRequirementMet": ratio_met,
        "requiredDemandCount": required_demand,
        "demandRequirementMet": demand_met,
        "advancementUnlocked": proposal_advancement_unlocked(
            signal_counts,
            required_demand=required_demand,
            uses_platform_vote_context=uses_platform_vote_context,
        ),
        "usesPlatformVoteContext": uses_platform_vote_context,
        "voteContextLabel": vote_context_label,
        "voteContextPopulation": vote_context_population,
    }


def ensure_proposal_advancement_allowed(
    signal_counts: Mapping[str, int],
    *,
    required_demand: int,
    uses_platform_vote_context: bool,
) -> None:
    if proposal_advancement_unlocked(
        signal_counts,
        required_demand=required_demand,
        uses_platform_vote_context=uses_platform_vote_context,
    ):
        return

    total = int(signal_counts.get("total") or 0)
    demand = int(signal_counts.get("demand") or 0)
    ratio_percent = (demand / total * 100.0) if total > 0 else 0.0

    if total <= 0:
        detail = "Proposal advancement requires active demand signals before planning can open."
    elif ratio_percent < SIGNAL_DEMAND_RATIO_THRESHOLD_PERCENT:
        detail = (
            "Proposal advancement requires demand to stay above "
            f"{int(SIGNAL_DEMAND_RATIO_THRESHOLD_PERCENT)}% of active signals."
        )
    elif uses_platform_vote_context and demand < required_demand:
        detail = (
            "Proposal advancement requires more demand signals from weekly active users "
            "before planning can open."
        )
    else:
        detail = "Proposal advancement signal requirements are not met."

    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
