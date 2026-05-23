from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import declarative_base

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
Base = declarative_base(metadata=metadata)
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB
TSVECTOR = postgresql.TSVECTOR


def uuid_pk() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def updated_at() -> sa.Column:
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def timestamp_columns(include_updated: bool = True):
    cols = [created_at()]
    if include_updated:
        cols.append(updated_at())
    return cols


def user_fk(name: str, *, nullable: bool = False, ondelete: str = "SET NULL") -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("users.id", ondelete=ondelete), nullable=nullable)


def project_fk(name: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("projects.id", ondelete=ondelete), nullable=nullable)


def event_fk(name: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("events.id", ondelete=ondelete), nullable=nullable)


def channel_fk(name: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("channels.id", ondelete=ondelete), nullable=nullable)


def community_fk(name: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey("communities.id", ondelete=ondelete), nullable=nullable)


def table(name: str, *columns, **kwargs) -> sa.Table:
    return sa.Table(name, metadata, *columns, **kwargs)
