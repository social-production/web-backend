"""Remove legacy region-map test pollution from a dev database.

Deletes rows created by the old test_region_map.py direct-insert seeds
(fake labels like Near Park / Far Place and slug prefixes near-evt-/far-evt-).
"""

from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, delete, or_, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.models import event_tags, events, help_requests, locations, users


LEGACY_LOCATION_LABELS = ("Near Park", "Far Place", "Secret Venue")
LEGACY_EVENT_SLUG_PREFIXES = ("near-evt-", "far-evt-", "priv-evt-")
LEGACY_USER_PREFIX = "geo-owner-"


def cleanup_test_pollution() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)

    with engine.begin() as conn:
        legacy_location_ids = [
            row[0]
            for row in conn.execute(
                select(locations.c.id).where(locations.c.display_label.in_(LEGACY_LOCATION_LABELS))
            ).all()
        ]

        legacy_event_ids = [
            row[0]
            for row in conn.execute(
                select(events.c.id).where(
                    or_(*[events.c.slug.like(f"{prefix}%") for prefix in LEGACY_EVENT_SLUG_PREFIXES])
                )
            ).all()
        ]

        legacy_user_ids = [
            row[0]
            for row in conn.execute(
                select(users.c.id).where(users.c.username.like(f"{LEGACY_USER_PREFIX}%"))
            ).all()
        ]

        if legacy_event_ids:
            conn.execute(delete(event_tags).where(event_tags.c.event_id.in_(legacy_event_ids)))
            conn.execute(delete(events).where(events.c.id.in_(legacy_event_ids)))

        conn.execute(
            delete(help_requests).where(help_requests.c.location_label.in_(LEGACY_LOCATION_LABELS))
        )

        if legacy_location_ids:
            conn.execute(delete(locations).where(locations.c.id.in_(legacy_location_ids)))

        if legacy_user_ids:
            conn.execute(delete(users).where(users.c.id.in_(legacy_user_ids)))

        print(
            "CLEANUP complete:",
            f"events={len(legacy_event_ids)}",
            f"locations={len(legacy_location_ids)}",
            f"users={len(legacy_user_ids)}",
        )


def main() -> None:
    cleanup_test_pollution()


if __name__ == "__main__":
    main()
