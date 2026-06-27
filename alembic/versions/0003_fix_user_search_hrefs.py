"""fix user search hrefs

Revision ID: 0003_fix_user_search_hrefs
Revises: 0002_subject_chat_reads
Create Date: 2026-06-26 00:00:01.000000
"""

from alembic import op


revision = "0003_fix_user_search_hrefs"
down_revision = "0002_subject_chat_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE searchable_documents
        SET href = '/profile/' || title
        WHERE entity_type = 'user' AND href LIKE '/users/%'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE searchable_documents
        SET href = '/users/' || title
        WHERE entity_type = 'user' AND href LIKE '/profile/%'
        """
    )
