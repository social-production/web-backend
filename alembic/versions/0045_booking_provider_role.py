"""Existing personal-service bookings get the provider's role, so the provider can rate them.

Revision ID: 0045_booking_provider_role
Revises: 0044_pending_invites
Create Date: 2026-10-18
"""

from alembic import op

revision = "0045_booking_provider_role"
down_revision = "0044_pending_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH bookings AS (
            SELECT a.id AS activity_id, p.author_id
            FROM project_activities a
            JOIN projects p ON p.id = a.project_id
            JOIN project_service_requests r ON r.linked_activity_id = a.id
            WHERE p.project_mode = 'personal-service'
              AND a.linked_request_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM project_activity_roles pr WHERE pr.activity_id = a.id
              )
        ),
        new_roles AS (
            INSERT INTO project_activity_roles (id, activity_id, label, required_count, maximum_count)
            SELECT gen_random_uuid(), activity_id, 'Provider', 1, 1 FROM bookings
            RETURNING id, activity_id
        )
        INSERT INTO project_activity_assignments (role_id, user_id)
        SELECT new_roles.id, bookings.author_id
        FROM new_roles JOIN bookings ON bookings.activity_id = new_roles.activity_id
        """
    )


def downgrade() -> None:
    pass
