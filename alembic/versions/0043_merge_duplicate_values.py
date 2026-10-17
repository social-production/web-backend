"""Merge same-named values on a project or event, keeping votes on the oldest.

Revision ID: 0043_merge_duplicate_values
Revises: 0042_plan_schedule_utc
Create Date: 2026-10-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0043_merge_duplicate_values"
down_revision = "0042_plan_schedule_utc"
branch_labels = None
depends_on = None

# (values table, owner column, importance votes, plan value votes, criterion ratings)
KINDS = (
    (
        "project_values",
        "project_id",
        "project_value_importance_votes",
        "project_plan_value_votes",
        "project_plan_criterion_ratings",
    ),
    (
        "event_values",
        "event_id",
        "event_value_importance_votes",
        "event_plan_value_votes",
        "event_plan_criterion_ratings",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    for values, owner, importance, plan_votes, ratings in KINDS:
        pairs = bind.execute(
            sa.text(
                f"""
                SELECT dup.id AS dup_id, keep.id AS keep_id
                FROM {values} dup
                JOIN LATERAL (
                    SELECT k.id FROM {values} k
                    WHERE k.{owner} = dup.{owner}
                      AND lower(btrim(k.label)) = lower(btrim(dup.label))
                    ORDER BY k.created_at, k.id
                    LIMIT 1
                ) keep ON keep.id <> dup.id
                """
            )
        ).all()
        for dup_id, keep_id in pairs:
            params = {"dup": dup_id, "keep": keep_id}
            # Where someone voted on both copies, the surviving value's vote stands.
            bind.execute(
                sa.text(
                    f"""
                    INSERT INTO {importance} (value_id, voter_id, importance, created_at)
                    SELECT :keep, voter_id, importance, created_at FROM {importance}
                    WHERE value_id = :dup
                    ON CONFLICT DO NOTHING
                    """
                ),
                params,
            )
            bind.execute(
                sa.text(
                    f"""
                    INSERT INTO {plan_votes} (plan_id, value_id, voter_id, vote, created_at)
                    SELECT plan_id, :keep, voter_id, vote, created_at FROM {plan_votes}
                    WHERE value_id = :dup
                    ON CONFLICT DO NOTHING
                    """
                ),
                params,
            )
            bind.execute(
                sa.text(
                    f"""
                    UPDATE {ratings} r SET criterion_id = 'value:' || CAST(:keep AS text)
                    WHERE r.criterion_id = 'value:' || CAST(:dup AS text)
                      AND NOT EXISTS (
                        SELECT 1 FROM {ratings} o
                        WHERE o.plan_id = r.plan_id AND o.voter_id = r.voter_id
                          AND o.criterion_id = 'value:' || CAST(:keep AS text)
                      )
                    """
                ),
                params,
            )
            bind.execute(
                sa.text(
                    f"DELETE FROM {ratings} WHERE criterion_id = 'value:' || CAST(:dup AS text)"
                ),
                params,
            )
            # Deleting the copy cascades its remaining votes.
            bind.execute(sa.text(f"DELETE FROM {values} WHERE id = :dup"), {"dup": dup_id})


def downgrade() -> None:
    pass
