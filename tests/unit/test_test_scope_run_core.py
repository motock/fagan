import subprocess
from pathlib import Path

import pytest

from pipeline.test_scope_run import (
    MAX_CI_FAILURE_TARGETS,
    NO_TESTS_CMD,
    ci_failure_targets,
    is_no_tests_cmd,
    is_scoped,
    scoped_test_cmd,
)

TEST_DIR = Path("tests")


@pytest.mark.parametrize(
    "story",
    [
        {"test_scope_resolution": {"test_scope": "scoped"}},
    ],
)
def test_is_scoped_true_when_resolution_is_scoped(story):
    assert is_scoped(story) is True


@pytest.mark.parametrize(
    "story",
    [
        {"test_scope_resolution": {"test_scope": "full"}},
        {},
        {"test_scope_resolution": None},
        {"test_scope_resolution": "scoped"},
        {"test_scope_resolution": {"test_scope": "Scoped"}},
        {"test_scope_resolution": {"test_scope": None}},
    ],
)
def test_is_scoped_false_for_anything_but_exact_scoped(story):
    assert is_scoped(story) is False


def test_ci_failure_targets_parses_pytest_short_summary_line():
    text = "FAILED tests/unit/test_a.py::test_x - assert 0"
    assert ci_failure_targets(text) == ["tests/unit/test_a.py::test_x"]


def test_ci_failure_targets_stops_node_id_at_reason_separator():
    text = "FAILED tests/unit/test_a.py::test_x[1-2] - assert 0 == 1"
    assert ci_failure_targets(text) == ["tests/unit/test_a.py::test_x[1-2]"]


@pytest.mark.parametrize(
    "hostile_id",
    [
        "../etc/test_x.py::t",
        "tests/../etc/test_x.py::t",
        "/abs/test_x.py",
        "-p evil.py",
        "com.x.FooTest.bar",
    ],
)
def test_ci_failure_targets_rejects_unsafe_or_non_pytest_ids(hostile_id):
    assert ci_failure_targets(f"FAILED {hostile_id} - boom") == []


def test_ci_failure_targets_none_yields_empty_list():
    assert ci_failure_targets(None) == []


def test_ci_failure_targets_caps_at_max_targets():
    text = "\n".join(
        f"FAILED tests/unit/test_{i}.py::test_x - assert 0" for i in range(250)
    )
    targets = ci_failure_targets(text)
    assert len(targets) == MAX_CI_FAILURE_TARGETS == 200


def test_ci_failure_targets_dedupes_preserving_order():
    text = (
        "FAILED tests/unit/test_b.py::test_y - assert 0\n"
        "FAILED tests/unit/test_a.py::test_x - assert 0\n"
        "FAILED tests/unit/test_b.py::test_y - assert 0\n"
    )
    assert ci_failure_targets(text) == [
        "tests/unit/test_a.py::test_x",
        "tests/unit/test_b.py::test_y",
    ]


def test_scoped_pytest_orders_acceptance_own_ci_and_drops_duplicates():
    cmd = scoped_test_cmd(
        ["pytest", "-q"],
        acceptance=["tests/acc/test_one.py", "tests/acc/test_two.py"],
        own=["tests/unit/test_own.py", "tests/acc/test_one.py"],
        ci_failures=["tests/unit/test_own.py::test_z", "tests/acc/test_two.py"],
        test_dir=TEST_DIR,
    )
    assert cmd == [
        "pytest",
        "-q",
        "tests/acc/test_one.py",
        "tests/acc/test_two.py",
        "tests/unit/test_own.py",
        "tests/unit/test_own.py::test_z",
    ]


def test_scoped_pytest_with_no_targets_returns_no_tests_cmd():
    cmd = scoped_test_cmd(
        ["pytest", "-q"],
        acceptance=[],
        own=[],
        ci_failures=[],
        test_dir=TEST_DIR,
    )
    assert cmd == NO_TESTS_CMD


def test_scoped_pytest_no_tests_result_is_a_new_list():
    cmd = scoped_test_cmd(
        ["pytest"], acceptance=[], own=[], ci_failures=[], test_dir=TEST_DIR
    )
    cmd.append("tests/unit/test_mutated.py")
    assert NO_TESTS_CMD[-1] != "tests/unit/test_mutated.py"


def test_scoped_maven_runs_only_acceptance_class():
    cmd = scoped_test_cmd(
        ["mvn", "test"],
        acceptance=["src/test/java/com/x/FooTest.java"],
        own=[],
        ci_failures=[],
        test_dir=TEST_DIR,
    )
    assert "-Dtest=com.x.FooTest" in cmd


def test_scoped_maven_without_acceptance_returns_no_tests_cmd():
    cmd = scoped_test_cmd(
        ["mvn", "test"],
        acceptance=[],
        own=["src/test/java/com/x/OwnTest.java"],
        ci_failures=[],
        test_dir=TEST_DIR,
    )
    assert cmd == NO_TESTS_CMD


def test_scoped_unscopable_runner_never_falls_back_to_full_suite():
    cmd = scoped_test_cmd(
        ["make", "test"],
        acceptance=["tests/acc/test_one.py"],
        own=[],
        ci_failures=[],
        test_dir=TEST_DIR,
    )
    assert cmd == NO_TESTS_CMD


def test_is_no_tests_cmd_true_for_sentinel():
    assert is_no_tests_cmd(NO_TESTS_CMD) is True


@pytest.mark.parametrize("cmd", [["pytest", "-q"], ["make", "test"], []])
def test_is_no_tests_cmd_false_for_real_commands(cmd):
    assert is_no_tests_cmd(cmd) is False


def test_no_tests_cmd_runs_and_exits_zero():
    assert subprocess.run(NO_TESTS_CMD, check=False).returncode == 0
