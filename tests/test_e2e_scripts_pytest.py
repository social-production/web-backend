from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).parent

IN_PROCESS_SCRIPTS = [
    "test_actions_search_notifications_e2e.py",
    "test_bootstrap_shape_e2e.py",
    "test_event_detail_shape_e2e.py",
    "test_feeds_e2e.py",
    "test_missing_endpoints_e2e.py",
    "test_new_writes_e2e.py",
    "test_personal_service_requests_e2e.py",
    "test_phase_naming_e2e.py",
    "test_project_detail_shape_e2e.py",
    "test_remaining_adapters_e2e.py",
]

EXTERNAL_SERVER_SCRIPTS = [
    "test_bootstrap_personal_platform_e2e.py",
    "test_events_plans_phases_e2e.py",
    "test_projects_software_e2e.py",
]


def _load_run(script_name: str) -> Callable[[], None]:
    module_name = f"_pytest_wrapped_{script_name.removesuffix('.py')}"
    module_path = TESTS_DIR / script_name
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load {script_name}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = getattr(module, "run", None)
    if not callable(run):
        raise AssertionError(f"{script_name} does not expose run()")
    return run


@pytest.mark.parametrize("script_name", IN_PROCESS_SCRIPTS)
def test_in_process_e2e_script(script_name: str) -> None:
    _load_run(script_name)()


@pytest.mark.parametrize("script_name", EXTERNAL_SERVER_SCRIPTS)
def test_external_server_e2e_script(script_name: str) -> None:
    if not os.environ.get("TEST_BASE_URL"):
        pytest.skip("Set TEST_BASE_URL to run external-server E2E scripts")
    _load_run(script_name)()
