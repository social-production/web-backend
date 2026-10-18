"""People can pick an accent colour and a font.

Revision ID: 0047_accent_and_font
Revises: 0046_combined_feed_default
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0047_accent_and_font"
down_revision = "0046_combined_feed_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("accent_color", sa.String(12), nullable=False, server_default="green"),
    )
    op.add_column(
        "user_settings",
        sa.Column("font_family", sa.String(12), nullable=False, server_default="inter"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "font_family")
    op.drop_column("user_settings", "accent_color")
