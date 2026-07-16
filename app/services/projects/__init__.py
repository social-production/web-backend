"""Project services package — lazy exports to avoid circular imports with software/phases."""

from __future__ import annotations

from typing import Any

__all__ = [
    "add_project_update",
    "add_project_value",
    "commit_project_activity_role",
    "create_project",
    "create_project_activity",
    "get_project_by_slug",
    "get_project_detail",
    "join_project",
    "leave_project",
    "share_project_with_user",
    "toggle_project_signal",
    "uncommit_project_activity_role",
    "update_project_details",
    "vote_project_value_importance",
    "_resolve_effective_project_subtype",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "add_project_update": ("app.services.projects.actions", "add_project_update"),
    "add_project_value": ("app.services.projects.actions", "add_project_value"),
    "commit_project_activity_role": (
        "app.services.projects.actions",
        "commit_project_activity_role",
    ),
    "create_project_activity": ("app.services.projects.actions", "create_project_activity"),
    "join_project": ("app.services.projects.actions", "join_project"),
    "leave_project": ("app.services.projects.actions", "leave_project"),
    "share_project_with_user": ("app.services.projects.actions", "share_project_with_user"),
    "toggle_project_signal": ("app.services.projects.actions", "toggle_project_signal"),
    "uncommit_project_activity_role": (
        "app.services.projects.actions",
        "uncommit_project_activity_role",
    ),
    "update_project_details": ("app.services.projects.actions", "update_project_details"),
    "vote_project_value_importance": (
        "app.services.projects.actions",
        "vote_project_value_importance",
    ),
    "get_project_detail": ("app.services.projects.detail", "get_project_detail"),
    "_resolve_effective_project_subtype": (
        "app.services.projects.helpers",
        "_resolve_effective_project_subtype",
    ),
    "create_project": ("app.services.projects.helpers", "create_project"),
    "get_project_by_slug": ("app.services.projects.helpers", "get_project_by_slug"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
