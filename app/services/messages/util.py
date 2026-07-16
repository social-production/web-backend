from __future__ import annotations


def _iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()
