from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, literal, not_, or_, select, union_all
from sqlalchemy.orm import Session


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
    event_plan_criterion_ratings,
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
    project_plan_criterion_ratings,
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
