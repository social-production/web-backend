from __future__ import annotations

import sqlalchemy as sa

from app.models.base import created_at, table, updated_at, uuid_pk

locations = table(
    "locations",
    uuid_pk(),
    sa.Column("provider_place_id", sa.String(160), nullable=True),
    sa.Column("display_label", sa.String(240), nullable=False),
    sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
    sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
    sa.Column("region", sa.String(120), nullable=True),
    sa.Column("country", sa.String(120), nullable=True),
    sa.Column("precision", sa.String(16), nullable=False, server_default="approximate"),
    sa.Column("is_online", sa.Boolean, nullable=False, server_default=sa.false()),
    created_at(),
    updated_at(),
    sa.CheckConstraint(
        "precision IN ('exact', 'approximate')",
        name="locations_precision",
    ),
    sa.CheckConstraint(
        "(is_online = TRUE) OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
        name="locations_coords_or_online",
    ),
    sa.Index("ix_locations_provider_place_id", "provider_place_id"),
    sa.Index("ix_locations_lat_lon", "latitude", "longitude"),
)
