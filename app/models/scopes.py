from __future__ import annotations

import sqlalchemy as sa

from app.models.base import (
    JSONB,
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
    sa.Column("image_storage_key", sa.Text, nullable=True),
    sa.Column("image_content_type", sa.String(127), nullable=True),
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
    sa.Column("image_storage_key", sa.Text, nullable=True),
    sa.Column("image_content_type", sa.String(127), nullable=True),
    # "collaborative" (members vote) or "organizer" (organizers edit; private communities only).
    sa.Column("governance", sa.String(16), nullable=False, server_default="collaborative"),
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

# A direct invite to a private community or event, waiting for the person to accept or decline.
pending_invites = table(
    "pending_invites",
    uuid_pk(),
    sa.Column("scope_kind", sa.String(16), nullable=False),
    sa.Column("scope_id", UUID, nullable=False),
    user_fk("user_id", nullable=False, ondelete="CASCADE"),
    user_fk("invited_by", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
    created_at(),
    sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
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


# Member-voted changes to a channel or community (app/services/scope_governance.py).
scope_proposals = table(
    "scope_proposals",
    uuid_pk(),
    sa.Column("scope_kind", sa.String(16), nullable=False),
    sa.Column("scope_id", UUID, nullable=False),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    user_fk("proposer_id", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
)

scope_proposal_votes = table(
    "scope_proposal_votes",
    sa.Column(
        "proposal_id",
        UUID,
        sa.ForeignKey("scope_proposals.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
