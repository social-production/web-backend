"""Backward-compatible re-exports for software project governance."""

from __future__ import annotations

from app.services.projects.software import (
    build_software_history_entries,
    get_project_software_governance,
    record_pull_request_merge,
    request_merge_capability_change,
    request_repository_replacement,
    submit_pull_request,
    sync_merge_capability_for_leading_plan,
    sync_platform_software_merge_capability,
    vote_merge_capability_change,
    vote_pull_request,
    vote_repository_replacement,
)

__all__ = [
    "build_software_history_entries",
    "get_project_software_governance",
    "record_pull_request_merge",
    "request_merge_capability_change",
    "request_repository_replacement",
    "submit_pull_request",
    "sync_merge_capability_for_leading_plan",
    "sync_platform_software_merge_capability",
    "vote_merge_capability_change",
    "vote_pull_request",
    "vote_repository_replacement",
]
