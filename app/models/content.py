from __future__ import annotations

import sqlalchemy as sa

from app.models.base import JSONB, UUID, channel_fk, community_fk, created_at, table, updated_at, user_fk, uuid_pk

posts = table(
    "posts",
    uuid_pk(),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("audience", sa.String(16), nullable=False),
    sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("comment_count", sa.Integer, nullable=False, server_default="0"),
    created_at(),
    updated_at(),
)

post_links = table(
    "post_links",
    uuid_pk(),
    sa.Column("post_id", UUID, sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
    sa.Column("subject_type", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("label", sa.String(200), nullable=False),
    sa.Column("href", sa.Text, nullable=False),
    created_at(),
)

threads = table(
    "threads",
    uuid_pk(),
    sa.Column("slug", sa.String(120), nullable=False, unique=True),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("comment_count", sa.Integer, nullable=False, server_default="0"),
    created_at(),
    updated_at(),
    sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
)

thread_tags = table(
    "thread_tags",
    uuid_pk(),
    sa.Column("thread_id", UUID, sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
    sa.Column("tag_kind", sa.String(16), nullable=False),
    channel_fk("channel_id", nullable=True),
    community_fk("community_id", nullable=True),
    sa.UniqueConstraint("thread_id", "tag_kind", "channel_id", "community_id", name="uq_thread_tags_tag"),
)

comments = table(
    "comments",
    uuid_pk(),
    sa.Column("subject_type", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("parent_id", UUID, sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=True),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
    created_at(),
    updated_at(),
)

content_votes = table(
    "content_votes",
    uuid_pk(),
    sa.Column("target_type", sa.String(16), nullable=False),
    sa.Column("target_id", UUID, nullable=False),
    user_fk("voter_id", nullable=False, ondelete="CASCADE"),
    sa.Column("direction", sa.SmallInteger, nullable=False),
    created_at(),
    updated_at(),
    sa.UniqueConstraint("target_type", "target_id", "voter_id", name="uq_content_votes_target_voter"),
)

reports = table(
    "reports",
    uuid_pk(),
    sa.Column("subject_type", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("target_type", sa.String(24), nullable=False),
    sa.Column("target_id", UUID, nullable=False),
    sa.Column("reason", sa.String(24), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    user_fk("reporter_id", nullable=True, ondelete="SET NULL"),
    user_fk("reported_author_id", nullable=True, ondelete="SET NULL"),
    sa.Column("resolution", sa.String(16), nullable=False, server_default="open"),
    created_at(),
    updated_at(),
    sa.UniqueConstraint("target_type", "target_id", name="uq_reports_target"),
)

report_votes = table(
    "report_votes",
    sa.Column("report_id", UUID, sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    created_at(),
)
