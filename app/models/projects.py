from __future__ import annotations

import sqlalchemy as sa

from app.models.base import JSONB, UUID, created_at, event_fk, project_fk, table, updated_at, user_fk, uuid_pk

projects = table(
    "projects",
    uuid_pk(),
    sa.Column("slug", sa.String(120), nullable=False, unique=True),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("project_mode", sa.String(32), nullable=False),
    sa.Column("project_subtype", sa.String(32), nullable=True),
    sa.Column("current_phase_id", sa.String(24), nullable=False),
    sa.Column("stage_label", sa.String(80), nullable=False),
    sa.Column("location_label", sa.String(160), nullable=False),
    sa.Column("is_platform_tagged", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("is_closed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("close_outcome", sa.String(16), nullable=True),
    sa.Column("signal_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("comment_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("member_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("land_asset_id", UUID, nullable=True),
    sa.Column("acquisition_id", UUID, nullable=True),
    created_at(),
    updated_at(),
    sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
)

project_memberships = table(
    "project_memberships",
    sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("is_manager", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("is_manager_candidate", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
)

project_tags = table(
    "project_tags",
    uuid_pk(),
    sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("tag_kind", sa.String(16), nullable=False),
    sa.Column("channel_id", UUID, sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=True),
    sa.Column("community_id", UUID, sa.ForeignKey("communities.id", ondelete="CASCADE"), nullable=True),
    sa.UniqueConstraint("project_id", "tag_kind", "channel_id", "community_id", name="uq_project_tags_tag"),
)

project_signals = table(
    "project_signals",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    user_fk("user_id", nullable=False, ondelete="CASCADE"),
    sa.Column("signal_type", sa.String(16), nullable=False),
    created_at(),
    sa.UniqueConstraint("project_id", "user_id", name="uq_project_signals_project_user"),
)

project_values = table(
    "project_values",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("label", sa.String(200), nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
)

project_value_importance_votes = table(
    "project_value_importance_votes",
    sa.Column("value_id", UUID, sa.ForeignKey("project_values.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("importance", sa.SmallInteger, nullable=False),
    created_at(),
)

project_plans = table(
    "project_plans",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("phase_kind", sa.String(32), nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("project_subtype", sa.String(32), nullable=True),
    sa.Column("repository_url", sa.Text, nullable=True),
    sa.Column("demand_consideration_note", sa.Text, nullable=False, server_default=""),
    sa.Column("total_cost_label", sa.String(80), nullable=True),
    sa.Column("plan_payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("is_leading", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
    updated_at(),
)

project_plan_votes = table(
    "project_plan_votes",
    sa.Column("plan_id", UUID, sa.ForeignKey("project_plans.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_plan_value_votes = table(
    "project_plan_value_votes",
    sa.Column("plan_id", UUID, sa.ForeignKey("project_plans.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("value_id", UUID, sa.ForeignKey("project_values.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_activities = table(
    "project_activities",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("linked_plan_id", UUID, sa.ForeignKey("project_plans.id", ondelete="SET NULL"), nullable=True),
    sa.Column("linked_plan_phase_id", sa.String(64), nullable=True),
    sa.Column("linked_request_id", UUID, nullable=True),
    sa.Column("title", sa.String(200), nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("location_label", sa.String(160), nullable=False),
    sa.Column("note", sa.Text, nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default="active"),
    created_at(),
    updated_at(),
)

project_activity_roles = table(
    "project_activity_roles",
    uuid_pk(),
    sa.Column("activity_id", UUID, sa.ForeignKey("project_activities.id", ondelete="CASCADE"), nullable=False),
    sa.Column("label", sa.String(100), nullable=False),
    sa.Column("required_count", sa.Integer, nullable=False),
    sa.Column("maximum_count", sa.Integer, nullable=True),
    created_at(),
)

project_activity_assignments = table(
    "project_activity_assignments",
    sa.Column("role_id", UUID, sa.ForeignKey("project_activity_roles.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    created_at(),
)

project_service_request_settings = table(
    "project_service_request_settings",
    sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("request_mode", sa.String(16), nullable=False, server_default="both"),
    sa.Column("allow_off_schedule_requests", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    updated_at(),
)

project_service_requests = table(
    "project_service_requests",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    user_fk("requester_id", nullable=True, ondelete="SET NULL"),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("linked_activity_id", UUID, sa.ForeignKey("project_activities.id", ondelete="SET NULL"), nullable=True),
    created_at(),
    updated_at(),
)

project_service_request_setting_changes = table(
    "project_service_request_setting_changes",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("request_mode", sa.String(16), nullable=False),
    sa.Column("allow_off_schedule_requests", sa.Boolean, nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

project_service_request_setting_change_votes = table(
    "project_service_request_setting_change_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("project_service_request_setting_changes.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_updates = table(
    "project_updates",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
)

project_update_requests = table(
    "project_update_requests",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

project_update_request_votes = table(
    "project_update_request_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("project_update_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_edit_requests = table(
    "project_edit_requests",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

project_edit_request_votes = table(
    "project_edit_request_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("project_edit_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_phase_change_requests = table(
    "project_phase_change_requests",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("from_phase_id", sa.String(24), nullable=False),
    sa.Column("target_phase_id", sa.String(24), nullable=False),
    sa.Column("change_kind", sa.String(16), nullable=False),
    sa.Column("close_outcome", sa.String(16), nullable=True),
    sa.Column("conversion_target_mode", sa.String(32), nullable=True),
    sa.Column("conversion_target_subtype", sa.String(32), nullable=True),
    sa.Column("reason", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

project_phase_change_votes = table(
    "project_phase_change_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("project_phase_change_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

project_service_history_completions = table(
    "project_service_history_completions",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("history_item_key", sa.String(120), nullable=False),
    sa.Column("requester_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("participant_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("completion_state", sa.String(16), nullable=False),
    created_at(),
    updated_at(),
    sa.CheckConstraint("(requester_user_id IS NOT NULL) <> (participant_user_id IS NOT NULL)", name="project_service_history_completions_one_actor"),
    sa.UniqueConstraint("project_id", "history_item_key", "role", "requester_user_id", "participant_user_id", name="uq_project_service_history_completions_key"),
)

project_revert_history = table(
    "project_revert_history",
    uuid_pk(),
    project_fk("project_id", nullable=False),
    sa.Column("target_phase_id", sa.String(24), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
)

project_links = table(
    "project_links",
    uuid_pk(),
    sa.Column("source_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("target_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("relationship_label", sa.String(120), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("link_kind", sa.String(24), nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default="active"),
    created_at(),
)

project_link_requests = table(
    "project_link_requests",
    uuid_pk(),
    sa.Column("source_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("target_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("relationship_label", sa.String(120), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    user_fk("proposed_by", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

project_link_request_votes = table(
    "project_link_request_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("project_link_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    sa.Column("vote_scope", sa.String(16), nullable=False, primary_key=True),
    created_at(),
)

project_conversions = table(
    "project_conversions",
    uuid_pk(),
    sa.Column("predecessor_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("successor_project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("inventory_note", sa.Text, nullable=False),
    sa.Column("permanence_note", sa.Text, nullable=False),
    created_at(),
)
