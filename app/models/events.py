from __future__ import annotations

import sqlalchemy as sa

from app.models.base import UUID, created_at, event_fk, table, updated_at, user_fk, uuid_pk

events = table(
    "events",
    uuid_pk(),
    sa.Column("slug", sa.String(120), nullable=False, unique=True),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("created_by", nullable=True, ondelete="SET NULL"),
    sa.Column("is_private", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("current_phase_id", sa.String(24), nullable=False),
    sa.Column("time_label", sa.String(120), nullable=False),
    sa.Column("location_label", sa.String(160), nullable=False),
    sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("comment_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("going_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("member_count", sa.Integer, nullable=False, server_default="0"),
    created_at(),
    updated_at(),
    sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
)

event_memberships = table(
    "event_memberships",
    sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.String(24), nullable=False, server_default="member"),
    sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
)

event_editors = table(
    "event_editors",
    sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    user_fk("granted_by", nullable=True, ondelete="SET NULL"),
    sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
)

event_attendance = table(
    "event_attendance",
    sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("attendance_state", sa.String(16), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

event_tags = table(
    "event_tags",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("tag_kind", sa.String(16), nullable=False),
    sa.Column("channel_id", UUID, sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=True),
    sa.Column("community_id", UUID, sa.ForeignKey("communities.id", ondelete="CASCADE"), nullable=True),
    sa.UniqueConstraint("event_id", "tag_kind", "channel_id", "community_id", name="uq_event_tags_tag"),
)

event_signals = table(
    "event_signals",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    user_fk("user_id", nullable=False, ondelete="CASCADE"),
    sa.Column("signal_type", sa.String(16), nullable=False),
    created_at(),
    sa.UniqueConstraint("event_id", "user_id", name="uq_event_signals_event_user"),
)

event_values = table(
    "event_values",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("label", sa.String(200), nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
)

event_value_importance_votes = table(
    "event_value_importance_votes",
    sa.Column("value_id", UUID, sa.ForeignKey("event_values.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("importance", sa.SmallInteger, nullable=False),
    created_at(),
)

event_plans = table(
    "event_plans",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("demand_consideration_note", sa.Text, nullable=False, server_default=""),
    sa.Column("location_label", sa.String(160), nullable=False),
    sa.Column("schedule_payload", sa.dialects.postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("plan_payload", sa.dialects.postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("is_leading", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

event_plan_votes = table(
    "event_plan_votes",
    sa.Column("plan_id", UUID, sa.ForeignKey("event_plans.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

event_plan_value_votes = table(
    "event_plan_value_votes",
    sa.Column("plan_id", UUID, sa.ForeignKey("event_plans.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("value_id", UUID, sa.ForeignKey("event_values.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

event_plan_criterion_ratings = table(
    "event_plan_criterion_ratings",
    sa.Column("plan_id", UUID, sa.ForeignKey("event_plans.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("criterion_id", sa.String(120), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("rating", sa.Integer, nullable=False),
    created_at(),
)

event_activities = table(
    "event_activities",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("linked_plan_id", UUID, sa.ForeignKey("event_plans.id", ondelete="SET NULL"), nullable=True),
    sa.Column("linked_plan_phase_id", sa.String(64), nullable=True),
    sa.Column("title", sa.String(200), nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("location_label", sa.String(160), nullable=False),
    sa.Column("note", sa.Text, nullable=False),
    created_at(),
)

event_activity_roles = table(
    "event_activity_roles",
    uuid_pk(),
    sa.Column("activity_id", UUID, sa.ForeignKey("event_activities.id", ondelete="CASCADE"), nullable=False),
    sa.Column("label", sa.String(100), nullable=False),
    sa.Column("required_count", sa.Integer, nullable=False),
    sa.Column("maximum_count", sa.Integer, nullable=True),
    created_at(),
)

event_activity_assignments = table(
    "event_activity_assignments",
    sa.Column("role_id", UUID, sa.ForeignKey("event_activity_roles.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    created_at(),
)

event_activity_ratings = table(
    "event_activity_ratings",
    sa.Column("activity_id", UUID, sa.ForeignKey("event_activities.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("rating", sa.Integer, nullable=False),
    sa.Column("comment", sa.Text, nullable=True),
    created_at(),
    updated_at(),
    sa.CheckConstraint("rating >= 1 AND rating <= 5", name="event_activity_ratings_rating_range"),
)

event_updates = table(
    "event_updates",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
)

event_update_requests = table(
    "event_update_requests",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

event_update_request_votes = table(
    "event_update_request_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("event_update_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

event_edit_requests = table(
    "event_edit_requests",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

event_edit_request_votes = table(
    "event_edit_request_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("event_edit_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)

event_phase_change_requests = table(
    "event_phase_change_requests",
    uuid_pk(),
    event_fk("event_id", nullable=False),
    sa.Column("from_phase_id", sa.String(24), nullable=False),
    sa.Column("target_phase_id", sa.String(24), nullable=False),
    sa.Column("change_kind", sa.String(16), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)

event_phase_change_votes = table(
    "event_phase_change_votes",
    sa.Column("request_id", UUID, sa.ForeignKey("event_phase_change_requests.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)
