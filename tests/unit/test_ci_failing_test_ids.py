"""Failing-test ids in the merge-gate CI rework message.

The CI-fail gate error embeds the failed run's log excerpt. When the log is
readable, the GitHub adapter prepends a ``Failing tests:`` block built from the
pytest ``FAILED <nodeid>`` lines, de-duplicated across matrix jobs and capped.
When the log cannot be read, the excerpt - and so the message - is unchanged.
"""

import logging
import subprocess

import pytest

from pipeline import advance_merge as adv_merge
from pipeline import server as p
from pipeline.ci_failing_tests import (
    FAILING_TESTS_CAP,
    extract_failing_tests,
    format_failing_tests,
)
from pipeline.forge_github import GitHubForge

BRANCH = "agent/h-2"
RUN_ID = "42"


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _failed_line(nodeid, summary="AssertionError: assert 1 == 2"):
    return f"FAILED {nodeid} - {summary}"


def _matrix_log(failed_lines_per_job):
    """Render ``gh run view --log-failed`` output: one prefixed block per job."""
    rows = []
    for job, lines in failed_lines_per_job:
        for line in lines:
            rows.append(f"{job}\tRun pytest\t2026-11-13T10:00:00.0000000Z {line}")
    return "\n".join(rows) + "\n"


def _install_gh(monkeypatch, log, *, run_list_rc=0, view_rc=0):
    """Route the two ``gh`` calls ``failure_log_excerpt`` makes."""

    def _fake_run(argv, **_):
        if argv[:3] == ["gh", "run", "list"]:
            return _Result(stdout=f"{RUN_ID}\n", returncode=run_list_rc)
        if argv[:3] == ["gh", "run", "view"]:
            return _Result(stdout=log, returncode=view_rc)
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(subprocess, "run", _fake_run)


# ---------------------------------------------------------------------------
# Parsing the FAILED lines
# ---------------------------------------------------------------------------
def test_extracts_nodeid_and_summary_from_pytest_short_summary():
    log = _matrix_log([("Test (Python 3.11)", [_failed_line("tests/unit/test_a.py::test_x")])])

    assert extract_failing_tests(log) == [
        ("tests/unit/test_a.py::test_x", "AssertionError: assert 1 == 2"),
    ]


def test_summary_is_capped_at_200_characters():
    long_summary = "E" * 500
    log = _matrix_log([("Test", [_failed_line("tests/unit/test_a.py::test_x", long_summary)])])

    ((_, summary),) = extract_failing_tests(log)

    assert len(summary) == 200


def test_line_without_summary_keeps_the_nodeid_alone():
    log = _matrix_log([("Test", ["FAILED tests/unit/test_a.py::test_x"])])

    assert extract_failing_tests(log) == [("tests/unit/test_a.py::test_x", "")]


def test_non_failed_lines_and_failed_words_without_a_nodeid_are_ignored():
    log = _matrix_log([
        ("Lint", ["ruff: FAILED to lint the repo", "1 failed, 10 passed in 2s"]),
    ])

    assert extract_failing_tests(log) == []


@pytest.mark.parametrize(
    "nodeid",
    [
        "/etc/passwd::test_x",
        "../outside/test_x.py::test_x",
        "tests/../../secrets.py::test_x",
        "C:\\outside\\test_x.py::test_x",
    ],
)
def test_nodeids_outside_the_repo_are_dropped(nodeid):
    log = _matrix_log([("Test", [_failed_line(nodeid)])])

    assert extract_failing_tests(log) == []


# ---------------------------------------------------------------------------
# De-duplication across the matrix
# ---------------------------------------------------------------------------
def test_same_failure_across_four_matrix_jobs_is_listed_once():
    shared = _failed_line("tests/unit/test_a.py::test_x")
    jobs = [
        (f"Test (Python 3.{minor})", [shared])
        for minor in (11, 12, 13, 14)
    ]
    log = _matrix_log(jobs)

    assert extract_failing_tests(log) == [
        ("tests/unit/test_a.py::test_x", "AssertionError: assert 1 == 2"),
    ]


def test_distinct_failures_keep_first_seen_order():
    log = _matrix_log([
        ("Test (3.11)", [_failed_line("tests/unit/test_b.py::test_b")]),
        ("Test (3.12)", [
            _failed_line("tests/unit/test_a.py::test_a"),
            _failed_line("tests/unit/test_b.py::test_b"),
        ]),
    ])

    assert [nodeid for nodeid, _ in extract_failing_tests(log)] == [
        "tests/unit/test_b.py::test_b",
        "tests/unit/test_a.py::test_a",
    ]


# ---------------------------------------------------------------------------
# Formatting and the cap
# ---------------------------------------------------------------------------
def _entries(count):
    return [(f"tests/unit/test_a.py::test_{i}", "AssertionError: boom") for i in range(count)]


def test_no_entries_formats_to_an_empty_block():
    assert format_failing_tests([]) == ""


def test_twenty_ids_are_all_listed_without_an_overflow_line():
    block = format_failing_tests(_entries(20))

    assert block.startswith("Failing tests:\n")
    assert block.count("\n- ") == 20
    assert "more" not in block


def test_twenty_first_id_is_cut_and_reported_as_one_more():
    block = format_failing_tests(_entries(21))

    assert block.count("\n- ") == FAILING_TESTS_CAP
    assert "test_19" in block
    assert "test_20" not in block
    assert block.endswith("\n... and 1 more")


def test_overflow_count_reflects_every_id_past_the_cap():
    block = format_failing_tests(_entries(27))

    assert block.endswith("\n... and 7 more")


# ---------------------------------------------------------------------------
# The GitHub adapter's excerpt, which the CI gate error embeds
# ---------------------------------------------------------------------------
def test_excerpt_leads_with_the_failing_tests_block(monkeypatch):
    log = _matrix_log([
        ("Test (3.11)", [_failed_line("tests/unit/test_a.py::test_x")]),
        ("Test (3.12)", [_failed_line("tests/unit/test_a.py::test_x")]),
    ])
    _install_gh(monkeypatch, log)

    excerpt = GitHubForge().failure_log_excerpt(BRANCH)

    assert excerpt.startswith(
        "Failing tests:\n- tests/unit/test_a.py::test_x - AssertionError: assert 1 == 2\n"
    )
    assert excerpt.count("\n- tests/unit/test_a.py::test_x") == 1


def test_excerpt_without_failed_lines_is_the_unchanged_log_tail(monkeypatch):
    log = "Test (3.11)\tRun pytest\tcollected 3 items\n" * 100
    _install_gh(monkeypatch, log)

    assert GitHubForge().failure_log_excerpt(BRANCH) == log[-800:]


def test_log_fetch_error_returns_none_and_logs_at_debug(monkeypatch, caplog):
    def _boom(argv, **_):
        raise OSError("gh not installed")

    monkeypatch.setattr(subprocess, "run", _boom)

    with caplog.at_level(logging.DEBUG, logger="pipeline.forge_github"):
        assert GitHubForge().failure_log_excerpt(BRANCH) is None

    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_run_list_api_error_returns_none(monkeypatch):
    _install_gh(monkeypatch, "", run_list_rc=1)

    assert GitHubForge().failure_log_excerpt(BRANCH) is None


# ---------------------------------------------------------------------------
# The rework message
# ---------------------------------------------------------------------------
def test_rework_message_keeps_the_marker_as_its_prefix_and_names_the_tests(monkeypatch):
    log = _matrix_log([("Test (3.11)", [_failed_line("tests/unit/test_a.py::test_x")])])
    _install_gh(monkeypatch, log)
    excerpt = GitHubForge().failure_log_excerpt(BRANCH)

    msg = p._ci_rework_feedback(f"ci fail: Test (Python 3.11): failure\n\n{excerpt}", 1)

    assert msg.startswith(adv_merge._CI_FEEDBACK_MARKER)
    assert "Failing tests:\n- tests/unit/test_a.py::test_x" in msg


def test_rework_message_without_logs_is_the_unchanged_message(monkeypatch):
    def _boom(argv, **_):
        raise OSError("gh not installed")

    monkeypatch.setattr(subprocess, "run", _boom)
    excerpt = GitHubForge().failure_log_excerpt(BRANCH)
    gate_error = "ci fail: Test (Python 3.11): failure"

    assert excerpt is None
    msg = p._ci_rework_feedback(gate_error, 1)
    assert "Failing tests:" not in msg
    assert msg.startswith(adv_merge._CI_FEEDBACK_MARKER)
    assert f"Gate error: {gate_error}" in msg
