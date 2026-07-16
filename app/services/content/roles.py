from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    help_request_role_assignments,
    help_request_roles,
)

VALID_AUDIENCE = frozenset({"public", "followers"})


def _load_help_request_roles(
    db: Session,
    help_request_ids: list[UUID],
    current_user_id: UUID | None = None,
) -> dict[str, list[dict[str, object]]]:
    if not help_request_ids:
        return {}

    filled_counts = dict(
        db.execute(
            select(
                help_request_role_assignments.c.role_id,
                func.count(help_request_role_assignments.c.user_id),
            )
            .where(
                help_request_role_assignments.c.role_id.in_(
                    select(help_request_roles.c.id).where(
                        help_request_roles.c.help_request_id.in_(help_request_ids)
                    )
                )
            )
            .group_by(help_request_role_assignments.c.role_id)
        ).all()
    )

    viewer_assignments: dict[UUID, UUID] = {}
    if current_user_id is not None:
        viewer_rows = db.execute(
            select(
                help_request_role_assignments.c.role_id,
                help_request_roles.c.help_request_id,
            )
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                )
            )
            .where(
                help_request_roles.c.help_request_id.in_(help_request_ids),
                help_request_role_assignments.c.user_id == current_user_id,
            )
        ).all()
        viewer_assignments = {hr_id: role_id for role_id, hr_id in viewer_rows}

    role_rows = (
        db.execute(
            select(
                help_request_roles.c.id,
                help_request_roles.c.help_request_id,
                help_request_roles.c.title,
                help_request_roles.c.description,
                help_request_roles.c.slots,
                help_request_roles.c.sort_order,
            )
            .where(help_request_roles.c.help_request_id.in_(help_request_ids))
            .order_by(help_request_roles.c.help_request_id, help_request_roles.c.sort_order.asc())
        )
        .mappings()
        .all()
    )

    result: dict[str, list[dict[str, object]]] = {}
    for row in role_rows:
        hr_id = str(row["help_request_id"])
        role_id = row["id"]
        filled_count = int(filled_counts.get(role_id, 0))
        result.setdefault(hr_id, []).append(
            {
                "role_id": role_id,
                "title": row["title"],
                "description": row["description"],
                "slots": int(row["slots"]),
                "filled_count": filled_count,
                "is_viewer_assigned": viewer_assignments.get(row["help_request_id"]) == role_id,
            }
        )
    return result


def _help_request_role_summaries(
    roles: list[dict[str, object]],
) -> tuple[int, int]:
    signed_up = sum(int(role.get("filled_count", 0)) for role in roles)
    needed = sum(int(role.get("slots", 0)) for role in roles)
    return signed_up, needed


load_help_request_roles = _load_help_request_roles
help_request_role_summaries = _help_request_role_summaries
