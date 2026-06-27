from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseCopy:
    summary: str
    mechanics: tuple[str, ...]
    note: str | None = None


DEFAULT_PROJECT_PHASE_COPY: dict[str, PhaseCopy] = {
    "phase-1": PhaseCopy(
        summary="Define what this project should achieve and how strongly members care about each value.",
        mechanics=(
            "Members propose values that describe outcomes the project should optimize for.",
            "Everyone rates each value's importance on a 1–10 scale; ratings above 50% carry into later planning.",
            "Demand and opposition signals show whether enough members want the project to move forward.",
            "Advancement unlocks when demand meets the quorum threshold for this project's vote context.",
        ),
    ),
    "phase-2": PhaseCopy(
        summary="Turn agreed values into a concrete production or operations plan.",
        mechanics=(
            "Members submit plans that explain how the project will be built, run, or delivered.",
            "Each plan must respond to the current demand signal and address high-importance values.",
            "Plans are voted on value-by-value and as a whole until one plan clears the approval threshold.",
            "The winning plan becomes the reference for activity scheduling and later phases.",
        ),
    ),
    "phase-3": PhaseCopy(
        summary="Decide how outputs reach the people who need them.",
        mechanics=(
            "Members propose distribution or access plans describing delivery, availability, and handoff.",
            "Plans are evaluated against the same high-importance values from proposal.",
            "Voting follows the same value-then-overall pattern used in the prior planning phase.",
            "The approved plan sets how the community shares or accesses what the project produces.",
        ),
    ),
    "phase-4": PhaseCopy(
        summary="Prepare inventory, materials, and acquisition steps before execution.",
        mechanics=(
            "Track what must be acquired or prepared before the project can execute at scale.",
            "Inventory and acquisition requests can be raised and voted on by members.",
            "This phase bridges planning and live activity for projects that need physical or staged setup.",
            "Advance when acquisition gates defined by the project are satisfied.",
        ),
    ),
    "phase-5": PhaseCopy(
        summary="Schedule and run the activities that deliver the approved plan.",
        mechanics=(
            "Activities are created on the calendar and tied to roles with minimum signup requirements.",
            "Members commit to roles; calendar color reflects whether minimums are met.",
            "Service requests and direct bookings may be available depending on the approved plan.",
            "Activities continue until the project is ready to close or convert.",
        ),
    ),
    "phase-6": PhaseCopy(
        summary="Confirm execution is complete and ready for final closure.",
        mechanics=(
            "Outstanding activities and execution items are reviewed before the project closes.",
            "Members can vote on whether execution obligations have been met.",
            "This phase prevents premature closure while work is still in progress.",
            "Advance to Closed once execution is confirmed or the community votes to close.",
        ),
    ),
    "phase-7": PhaseCopy(
        summary="The project has finished its lifecycle and is archived for reference.",
        mechanics=(
            "No new plans, activities, or phase changes are accepted.",
            "History, votes, and outcomes remain visible for accountability.",
            "Productive projects may convert into collective-service successors from the prior close vote.",
            "Members can still browse updates and past decisions.",
        ),
    ),
}

COLLECTIVE_SERVICE_OVERRIDES: dict[str, PhaseCopy] = {
    "phase-2": PhaseCopy(
        summary="Define how the collective service will operate day to day.",
        mechanics=(
            "Submit operations plans covering staffing, scheduling, and how the service runs.",
            "Explain how the plan meets current demand and each high-importance value.",
            "Members vote on value fit and overall approval until one plan leads.",
            "The winning operations plan drives activity templates and request handling.",
        ),
    ),
    "phase-3": PhaseCopy(
        summary="Decide who can access the service and under what conditions.",
        mechanics=(
            "Propose access plans describing eligibility, booking rules, and capacity limits.",
            "Plans must align with values rated above 50% in proposal.",
            "Approval uses the same voting mechanics as the operations-plan phase.",
            "The access plan governs how members and guests request the service.",
        ),
    ),
}

PERSONAL_SERVICE_OVERRIDES: dict[str, PhaseCopy] = {
    "phase-1": PhaseCopy(
        summary="Offer your personal service and manage incoming requests.",
        mechanics=(
            "You define availability, roles, and how clients can book time with you.",
            "Requests appear on your calendar; you accept, plan, or decline them directly.",
            "No multi-member governance voting is required for standard personal-service flow.",
            "Close the service when you are no longer accepting bookings.",
        ),
        note="Personal services skip collective proposal and multi-plan voting.",
    ),
    "phase-2": PhaseCopy(
        summary="Your personal service is closed and no longer accepting activity.",
        mechanics=(
            "Existing history and past bookings remain visible.",
            "You can reopen by creating a new service listing if the platform allows it.",
            "Clients cannot submit new requests while the service is closed.",
        ),
    ),
}

DEFAULT_EVENT_PHASE_COPY: dict[str, PhaseCopy] = {
    "proposal": PhaseCopy(
        summary="Gather interest and define the values this event should optimize for.",
        mechanics=(
            "Members propose values and rate their importance; scores above 50% feed into planning.",
            "Demand signals show whether enough people want the event to proceed.",
            "Editors and members align on scope before detailed plans are submitted.",
            "Advance when demand and value agreement meet the event's thresholds.",
        ),
    ),
    "event-plan": PhaseCopy(
        summary="Propose schedules, locations, and staged plans for the event.",
        mechanics=(
            "Members submit event plans with dates, times, location, and staged activities.",
            "Each plan must explain how it responds to demand and high-importance values.",
            "Plans are voted on per value and overall until one clears approval.",
            "The winning plan sets the live event title, schedule, and activity calendar bounds.",
        ),
    ),
    "activity": PhaseCopy(
        summary="Run scheduled event activities and fill roles.",
        mechanics=(
            "Activities are scheduled on approved plan days with role minimums.",
            "Signup progress is shown on the calendar: empty, partial, or minimum met.",
            "Members commit to roles; chat and updates stay tied to the event.",
            "The event moves to Closed when activities finish or organizers close it.",
        ),
    ),
    "closed": PhaseCopy(
        summary="The event has ended; outcomes and history remain visible.",
        mechanics=(
            "No new plans or activities can be added.",
            "Votes, attendance, and updates are preserved for reference.",
            "Members can review what was decided and what ran.",
        ),
    ),
}


def project_phase_copy(
    phase_id: str,
    project_mode: str,
    default_summary: str,
) -> dict[str, object]:
    if project_mode == "personal-service" and phase_id in PERSONAL_SERVICE_OVERRIDES:
        copy = PERSONAL_SERVICE_OVERRIDES[phase_id]
    elif project_mode == "collective-service" and phase_id in COLLECTIVE_SERVICE_OVERRIDES:
        copy = COLLECTIVE_SERVICE_OVERRIDES[phase_id]
    elif phase_id in DEFAULT_PROJECT_PHASE_COPY:
        copy = DEFAULT_PROJECT_PHASE_COPY[phase_id]
    else:
        copy = PhaseCopy(summary=default_summary, mechanics=())

    return {
        "summary": copy.summary,
        "mechanics": list(copy.mechanics),
        "note": copy.note,
    }


def event_phase_copy(phase_id: str, default_summary: str) -> dict[str, object]:
    if phase_id in DEFAULT_EVENT_PHASE_COPY:
        copy = DEFAULT_EVENT_PHASE_COPY[phase_id]
    else:
        copy = PhaseCopy(summary=default_summary, mechanics=())

    return {
        "summary": copy.summary,
        "mechanics": list(copy.mechanics),
        "note": copy.note,
    }
