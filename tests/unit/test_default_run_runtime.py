"""Guard the fast default local run and CI's unchanged coverage.

The slowest costs in a bare ``pytest`` were one real ``docker build`` (~160s)
and several tests that wait out the 30s plan-lock command wait. The first is
now behind a ``docker`` marker excluded from the default run; the second is
shortened for the whole suite. Excluding a test from the default run is only
safe while CI still runs it, so CI's full-suite step must clear the exclusion.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

from pipeline import concurrency

REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_SUITE_PREFIX = "pytest -q --override-ini=testpaths=."


def _pytest_ini_options() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]


def _load_dockerfile_test_module():
    path = REPO_ROOT / "tests" / "unit" / "test_dockerfile.py"
    spec = importlib.util.spec_from_file_location("_dockerfile_tests_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_should_default_command_lock_wait_to_a_short_suite_value() -> None:
    assert concurrency.command_lock_wait_seconds() <= 1.0


def test_should_register_the_docker_marker() -> None:
    markers = _pytest_ini_options()["markers"]

    assert any(marker.startswith("docker:") for marker in markers)


def test_should_exclude_docker_marked_tests_from_the_default_run() -> None:
    assert '-m "not docker"' in _pytest_ini_options()["addopts"]


def test_should_mark_the_real_docker_build_test_as_docker() -> None:
    module = _load_dockerfile_test_module()

    marks = getattr(module.test_docker_build_and_mcp_introspection, "pytestmark", [])

    assert "docker" in {mark.name for mark in marks}


def test_should_not_mark_the_cheap_dockerfile_tests_as_docker() -> None:
    module = _load_dockerfile_test_module()

    marks = getattr(module.test_dockerfile_exists_and_is_not_empty, "pytestmark", [])

    assert "docker" not in {mark.name for mark in marks}


def test_should_clear_the_docker_exclusion_in_ci_full_suite_step() -> None:
    ci_lines = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text().splitlines()
    full_suite = [line for line in ci_lines if FULL_SUITE_PREFIX in line]

    assert len(full_suite) == 1
    assert '-m ""' in full_suite[0]
