"""Edit requests store only the fields they change (NULL means unchanged).

Revision ID: 0041_edit_request_partial
Revises: 0040_scope_settings
Create Date: 2026-10-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0041_edit_request_partial"
down_revision = "0040_scope_settings"
branch_labels = None
depends_on = None

TABLES = ("project_edit_requests", "event_edit_requests")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "title", existing_type=sa.String(200), nullable=True)
        op.alter_column(table, "description", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"UPDATE {table} SET title = '' WHERE title IS NULL")
        op.execute(f"UPDATE {table} SET description = '' WHERE description IS NULL")
        op.alter_column(table, "title", existing_type=sa.String(200), nullable=False)
        op.alter_column(table, "description", existing_type=sa.Text(), nullable=False)
