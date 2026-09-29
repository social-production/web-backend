"""Postgres bytea BlobStore. The caller stores ciphertext, not plaintext."""

from __future__ import annotations

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import blobs


class PostgresBlobStore:
    def __init__(self, db: Session) -> None:
        self._db = db

    def put(self, key: str, data: bytes) -> None:
        self._db.execute(insert(blobs).values(storage_key=key, ciphertext=data))

    def get(self, key: str) -> bytes | None:
        row = self._db.execute(
            select(blobs.c.ciphertext).where(blobs.c.storage_key == key).limit(1)
        ).first()
        if row is None or row[0] is None:
            return None
        return bytes(row[0])

    def delete(self, key: str) -> None:
        self._db.execute(delete(blobs).where(blobs.c.storage_key == key))
