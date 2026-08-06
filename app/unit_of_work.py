"""Explicit transaction / unit-of-work helpers.

Prefer these over ad-hoc ``db.commit()`` so a future provider can own commit
semantics at the boundary.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def transaction(db: Session) -> Generator[Session, None, None]:
    """Commit on success, roll back on error."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def commit(db: Session) -> None:
    """Commit the current unit of work."""
    db.commit()


def rollback(db: Session) -> None:
    """Roll back the current unit of work."""
    db.rollback()
