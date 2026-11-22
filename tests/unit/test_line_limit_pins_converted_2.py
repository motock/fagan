"""Guards the hard line-limit pins converted in the second line-limit batch.

Each converted test must route its size check through assert_within_line_limit
(or line_limit_gate_mode for the checker script) so that the effective
PIPELINE_FILE_SIZE_POLICY decides whether an over-limit file fails. Each file
is read as source text and checked for the helper import, the helper call and
the absence of the hard assertion it replaced.
"""

import warnings
from pathlib import Path

import pytest

from tests.unit._line_limit_policy import (
    FILE_SIZE_POLICY_ENV,
    assert_within_line_limit,
)

TESTS_DIR = Path(__file__).resolve().parent

# (test file, expected helper call, the hard assert it replaced)
CONVERTED_PINS = [
    (
        "test_access_hooks.py",
        "assert_within_line_limit(len(lines), 1000, relative)",
        "assert len(lines) <= 1000",
    ),
    (
        "test_dispatch_plan_flock_guard.py",
        'assert_within_line_limit(count, checker.DEFAULT_LIMIT - 1, "pipeline/dispatch.py")',
        "assert count < checker.DEFAULT_LIMIT, (",
    ),
    (
        "test_dispatch_revised_note_split.py",
        'assert_within_line_limit(lines, LIMIT - 1, "pipeline/dispatch.py")',
        "assert lines < LIMIT\n",
    ),
    (
        "test_merge_bound_records.py",
        'assert_within_line_limit(len(source.splitlines()), 1000, "pipeline/advance_merge.py")',
        "assert len(source.splitlines()) <= 1000",
    ),
    (
        "test_role_routing_entry_points.py",
        "assert_within_line_limit(lines, gate.DEFAULT_LIMIT, Path(module.__file__).name)",
        "assert lines <= gate.DEFAULT_LIMIT, (",
    ),
    (
        "test_scheduler_daemon_surface.py",
        "assert_within_line_limit(line_count, 1000, DAEMON_PATH.name)",
        "assert line_count <= 1000, f",
    ),
    (
        "test_story_events_tool.py",
        'assert_within_line_limit(len(Path(server.__file__).read_text().splitlines()), 999, "pipeline/server.py")',
        "assert len(Path(server.__file__).read_text().splitlines()) < 1000",
    ),
    (
        "test_story_evidence_tools.py",
        'assert_within_line_limit(len(Path(server.__file__).read_text().splitlines()), 999, "pipeline/server.py")',
        "assert len(Path(server.__file__).read_text().splitlines()) < 1000",
    ),
    (
        "test_dashboard_split.py",
        "check_line_limit.check(REPO_ROOT, mode=line_limit_gate_mode()) == 0",
        "check_line_limit.check(REPO_ROOT) == 0",
    ),
]

IDS = [name for name, _, _ in CONVERTED_PINS]


@pytest.mark.parametrize(("filename", "helper_call", "replaced_assert"), CONVERTED_PINS, ids=IDS)
def test_size_pin_routes_through_line_limit_helper(filename, helper_call, replaced_assert):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")

    assert helper_call in source


@pytest.mark.parametrize(("filename", "helper_call", "replaced_assert"), CONVERTED_PINS, ids=IDS)
def test_size_pin_no_longer_uses_hard_assert(filename, helper_call, replaced_assert):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")

    assert replaced_assert not in source


@pytest.mark.parametrize(("filename", "helper_call", "replaced_assert"), CONVERTED_PINS, ids=IDS)
def test_size_pin_file_imports_the_helper(filename, helper_call, replaced_assert):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")

    assert "assert_within_line_limit" in source
    assert "from tests.unit._line_limit_policy import" in source


def test_real_repo_checker_run_routes_mode_through_gate_helper():
    source = (TESTS_DIR / "test_check_line_limit.py").read_text(encoding="utf-8")

    assert "from tests.unit._line_limit_policy import line_limit_gate_mode" in source
    assert '"--mode", line_limit_gate_mode()' in source
    assert '"--root", str(REPO_ROOT)],' not in source


def test_helper_warns_without_raising_when_over_limit_and_policy_unset(monkeypatch):
    monkeypatch.delenv(FILE_SIZE_POLICY_ENV, raising=False)

    with pytest.warns(UserWarning, match="1001 lines, over the 1000-line limit"):
        assert_within_line_limit(1001, 1000, "pipeline/example.py")


def test_helper_raises_when_over_limit_and_policy_enforced(monkeypatch):
    monkeypatch.setenv(FILE_SIZE_POLICY_ENV, "enforced")

    with pytest.raises(AssertionError, match="pipeline/example.py is 1001 lines"):
        assert_within_line_limit(1001, 1000, "pipeline/example.py")


@pytest.mark.parametrize("policy", ["advisory", "enforced"])
def test_helper_is_silent_at_exactly_the_limit(monkeypatch, policy):
    monkeypatch.setenv(FILE_SIZE_POLICY_ENV, policy)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_within_line_limit(1000, 1000, "pipeline/example.py")

    assert caught == []
