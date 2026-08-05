"""Add canonical locations table and entity location FKs.

Creates reusable `locations` records (provider place id, label, lat/lon,
region/country, precision, online) and optional `location_id` foreign keys on
events, projects, help requests, plans, and activities. Adds optional
`user_settings.default_location_id` for signed-in default place.

Revision ID: 0020_location_foundation
Revises: 0019_event_audience_governance
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020_location_foundation"
down_revision = "0019_event_audience_governance"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider_place_id", sa.String(160), nullable=True),
        sa.Column("display_label", sa.String(240), nullable=False),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("region", sa.String(120), nullable=True),
        sa.Column("country", sa.String(120), nullable=True),
        sa.Column("precision", sa.String(16), nullable=False, server_default="approximate"),
        sa.Column("is_online", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "precision IN ('exact', 'approximate')",
            name="ck_locations_locations_precision",
        ),
        sa.CheckConstraint(
            "(is_online = TRUE) OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="ck_locations_locations_coords_or_online",
        ),
    )
    op.create_index("ix_locations_provider_place_id", "locations", ["provider_place_id"])
    op.create_index("ix_locations_lat_lon", "locations", ["latitude", "longitude"])

    entity_tables = (
        "events",
        "projects",
        "help_requests",
        "event_plans",
        "project_plans",
        "event_activities",
        "project_activities",
    )
    for table_name in entity_tables:
        op.add_column(table_name, sa.Column("location_id", UUID, nullable=True))
        op.create_foreign_key(
            f"fk_{table_name}_location_id_locations",
            table_name,
            "locations",
            ["location_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.add_column("user_settings", sa.Column("default_location_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_user_settings_default_location_id_locations",
        "user_settings",
        "locations",
        ["default_location_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_user_settings_default_location_id_locations",
        "user_settings",
        type_="foreignkey",
    )
    op.drop_column("user_settings", "default_location_id")

    entity_tables = (
        "project_activities",
        "event_activities",
        "project_plans",
        "event_plans",
        "help_requests",
        "projects",
        "events",
    )
    for table_name in entity_tables:
        op.drop_constraint(
            f"fk_{table_name}_location_id_locations",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "location_id")

    op.drop_index("ix_locations_lat_lon", table_name="locations")
    op.drop_index("ix_locations_provider_place_id", table_name="locations")
    op.drop_table("locations")
