from __future__ import annotations

import sqlalchemy as sa

from app.models.base import (
    UUID,
    created_at,
    table,
    updated_at,
    user_fk,
    uuid_pk,
)

channels = table(
    "channels",
    uuid_pk(),
    sa.Column("slug", sa.String(80), nullable=False, unique=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("created_by", nullable=True, ondelete="SET NULL"),
    created_at(),
    updated_at(),
)

communities = table(
    "communities",
    uuid_pk(),
    sa.Column("slug", sa.String(80), nullable=False, unique=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("join_policy", sa.String(16), nullable=False),
    user_fk("created_by", nullable=True, ondelete="SET NULL"),
    created_at(),
    updated_at(),
)

scope_memberships = table(
    "scope_memberships",
    uuid_pk(),
    sa.Column("scope_kind", sa.String(16), nullable=False),
    sa.Column("scope_id", UUID, nullable=True),
    user_fk("user_id", nullable=False, ondelete="CASCADE"),
    sa.Column("role", sa.String(32), nullable=False, server_default="member"),
    created_at(),
    sa.UniqueConstraint(
        "scope_kind", "scope_id", "user_id", name="uq_scope_memberships_scope_user"
    ),
)

scope_invites = table(
    "scope_invites",
    uuid_pk(),
    sa.Column("scope_kind", sa.String(16), nullable=False),
    sa.Column("scope_id", UUID, nullable=True),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    user_fk("created_by", nullable=True, ondelete="SET NULL"),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("max_uses", sa.Integer, nullable=True),
    sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
    created_at(),
)

scope_confidence_votes = table(
    "scope_confidence_votes",
    sa.Column(
        "target_user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("scope_kind", sa.String(16), primary_key=True),
    sa.Column("scope_id", UUID, primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
    updated_at(),
    sa.CheckConstraint("target_user_id <> voter_id", name="scope_confidence_votes_not_self"),
)
