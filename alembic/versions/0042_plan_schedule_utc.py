"""Store event plan schedule dates and times in UTC.

Revision ID: 0042_plan_schedule_utc
Revises: 0041_edit_request_partial
Create Date: 2026-10-17
"""

import json

import sqlalchemy as sa

from alembic import op
from app.utils.plan_schedule import schedule_to_utc

revision = "0042_plan_schedule_utc"
down_revision = "0041_edit_request_partial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, schedule_payload FROM event_plans "
            "WHERE schedule_payload ? 'timeZone' AND schedule_payload->>'timeZone' <> 'UTC'"
        )
    ).all()
    for row in rows:
        converted = schedule_to_utc(dict(row.schedule_payload or {}))
        bind.execute(
            sa.text(
                "UPDATE event_plans SET schedule_payload = CAST(:payload AS jsonb) WHERE id = :id"
            ),
            {"payload": json.dumps(converted), "id": row.id},
        )


def downgrade() -> None:
    pass
