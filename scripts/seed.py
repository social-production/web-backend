from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine

# Ensure "app" imports work when running: python scripts/seed.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.models.scopes import channels


PLATFORM_SLUG = "platform"


def seed_platform_channel() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)

    with engine.begin() as conn:
        existing_id = conn.execute(
            sa.select(channels.c.id).where(channels.c.slug == PLATFORM_SLUG).limit(1)
        ).scalar_one_or_none()

        if existing_id is not None:
            print(f"SKIPPED channels slug={PLATFORM_SLUG} (already exists)")
            return

        conn.execute(
            sa.insert(channels).values(
                slug=PLATFORM_SLUG,
                name="Platform",
                description="The platform governance channel",
                created_by=None,
            )
        )
        print(f"CREATED channels slug={PLATFORM_SLUG}")


def main() -> None:
    seed_platform_channel()


if __name__ == "__main__":
    main()
