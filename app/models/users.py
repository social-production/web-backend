from __future__ import annotations

import sqlalchemy as sa

from app.models.base import JSONB, UUID, created_at, table, updated_at, user_fk, uuid_pk

users = table(
    "users",
    uuid_pk(),
    sa.Column("username", sa.String(32), nullable=False, unique=True),
    sa.Column("email", sa.String(320), nullable=True, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("bio", sa.Text, nullable=True),
    sa.Column("profile_image_url", sa.Text, nullable=True),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    created_at(),
    updated_at(),
)

user_settings = table(
    "user_settings",
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("appearance_theme_mode", sa.String(10), nullable=False, server_default="light"),
    sa.Column("default_feed", sa.String(10), nullable=False, server_default="public"),
    sa.Column("public_feed_scope", sa.String(16), nullable=False, server_default="global"),
    sa.Column("public_feed_filter", sa.String(16), nullable=False, server_default="all"),
    sa.Column("public_feed_sort", sa.String(16), nullable=False, server_default="popular"),
    sa.Column("public_feed_window", sa.String(8), nullable=False, server_default="all"),
    sa.Column("personal_feed_scope", sa.String(16), nullable=False, server_default="popular"),
    sa.Column("personal_feed_filter", sa.String(16), nullable=False, server_default="all"),
    sa.Column("personal_feed_sort", sa.String(16), nullable=False, server_default="popular"),
    sa.Column("personal_feed_window", sa.String(8), nullable=False, server_default="all"),
    sa.Column(
        "hide_public_activity_from_personal_feeds",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "hide_personal_feed_from_non_followers",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "hide_public_profile_activity_from_non_followers",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("require_follow_approval", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("preferred_language", sa.String(5), nullable=False, server_default="en"),
    sa.Column("display_timezone", sa.String(64), nullable=True),
    updated_at(),
)

user_follows = table(
    "user_follows",
    sa.Column("follower_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("followed_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("status", sa.String(16), nullable=False, server_default="accepted"),
    created_at(),
    sa.CheckConstraint("follower_id <> followed_id", name="user_follows_not_self"),
)

meaningful_actions = table(
    "meaningful_actions",
    uuid_pk(),
    user_fk("user_id", nullable=False, ondelete="CASCADE"),
    sa.Column("action_type", sa.String(32), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
)
