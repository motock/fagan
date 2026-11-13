"""`check_story_status` grades a scoped story on its story scope.

In `test_scope: scoped` the done-bar runs only the story's acceptance paths,
the test files it added or changed, and (on a `ci_rework`) the failing test
ids from the CI log. When nothing is in scope the host runs no tests and the
story records `test_scope_grade.no_tests`. Full mode must keep today's command
byte-for-byte; the full-mode pins below guard that.
"""

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import server as p
from pipeline import story_status
from pipeline import test_scope_run as tsr
from tests.unit._line_limit_policy import assert_within_line_limit

LINT_CMD = ["ruff", "check", "."]
REPO_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = [{"path": "tests/acceptance_x.py", "source": "def test_x(): pass\n"}]
CI_FEEDBACK = "FAILED tests/unit/test_a.py::test_x - boom"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _read_story(plan_dir):
    manifest = json.loads((plan_dir / "tsc.manifest.json").read_text())
    return manifest["stories"]["S1"]


def _setup(plan_dir, monkeypatch, *, story_extra=None, detected=None,
           lint_returncode=0, own=None):
    """Build a worktree + manifest and stub the grade's external boundaries.

    Returns the worktree and the list of commands run as the test grade (lint
    and git calls are excluded), so a test can assert the exact grading command.
    """
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    story = {"summary": "thing", "status": "in_progress",
             "pid": 4242, "worktree": str(worktree)}
    story.update(story_extra or {})
    (plan_dir / "tsc.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2)
    )
    monkeypatch.setattr(
        p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        p, "detect_test_command", lambda wt: (wt, detected or ["pytest", "-q"]),
    )
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: (wt, list(LINT_CMD)))
    monkeypatch.setattr(tsr, "changed_test_paths", lambda wt, base: list(own or []))

    test_runs = []

    def run_mock(cmd, **kwargs):
        if list(cmd) == LINT_CMD:
            return subprocess.CompletedProcess(cmd, lint_returncode, stdout="", stderr="")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        test_runs.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return worktree, test_runs


# ---------- Full-mode pins: byte-for-byte today's command ----------

@pytest.mark.parametrize("resolution", [
    None,
    {"test_scope": "full"},
], ids=["no-resolution", "explicit-full"])
def test_full_mode_acceptance_story_grades_acceptance_scoped_pytest_cmd(
    plan_dir, monkeypatch, resolution,
):
    extra = {"acceptance": ACCEPTANCE}
    if resolution is not None:
        extra["test_scope_resolution"] = resolution
    worktree, test_runs = _setup(plan_dir, monkeypatch, story_extra=extra)

    p.check_story_status("tsc", "S1")

    assert test_runs[0] == ["pytest", "-q", str(worktree / "tests/acceptance_x.py")]


@pytest.mark.parametrize("resolution", [
    None,
    {"test_scope": "full"},
], ids=["no-resolution", "explicit-full"])
def test_full_mode_story_without_acceptance_grades_detected_cmd(
    plan_dir, monkeypatch, resolution,
):
    extra = {}
    if resolution is not None:
        extra["test_scope_resolution"] = resolution
    _, test_runs = _setup(plan_dir, monkeypatch, story_extra=extra, detected=["make", "test"])

    p.check_story_status("tsc", "S1")

    assert test_runs[0] == ["make", "test"]


def test_full_mode_never_records_test_scope_grade(plan_dir, monkeypatch):
    _setup(plan_dir, monkeypatch,
           story_extra={"test_scope_resolution": {"test_scope": "full"}})

    p.check_story_status("tsc", "S1")

    assert "test_scope_grade" not in _read_story(plan_dir)


# ---------- Scoped: acceptance + own tests ----------

def test_scoped_acceptance_story_grades_acceptance_and_own_tests(plan_dir, monkeypatch):
    worktree, test_runs = _setup(
        plan_dir, monkeypatch,
        story_extra={"acceptance": ACCEPTANCE,
                     "test_scope_resolution": {"test_scope": "scoped"}},
        own=["tests/test_own.py"],
    )

    result = p.check_story_status("tsc", "S1")

    assert test_runs[0] == [
        "pytest", "-q",
        str(worktree / "tests/acceptance_x.py"),
        str(worktree / "tests/test_own.py"),
    ]
    assert result["status"] == "tests_passed"
    grade = _read_story(plan_dir)["test_scope_grade"]
    assert grade["targets"] == ["tests/acceptance_x.py", "tests/test_own.py"]
    assert grade["no_tests"] is False


# ---------- Scoped: nothing in scope ----------

def test_scoped_story_with_nothing_in_scope_grades_no_tests_cmd(plan_dir, monkeypatch):
    _, test_runs = _setup(plan_dir, monkeypatch,
                          story_extra={"test_scope_resolution": {"test_scope": "scoped"}})

    result = p.check_story_status("tsc", "S1")

    assert test_runs[0] == tsr.NO_TESTS_CMD
    assert result["status"] == "tests_passed"


def test_scoped_story_with_nothing_in_scope_records_no_tests(plan_dir, monkeypatch):
    _setup(plan_dir, monkeypatch,
           story_extra={"test_scope_resolution": {"test_scope": "scoped"}})

    p.check_story_status("tsc", "S1")

    assert _read_story(plan_dir)["test_scope_grade"]["no_tests"] is True


@pytest.mark.parametrize("lint_returncode, expected_status", [
    (0, "tests_passed"),
    (1, "failed"),
], ids=["lint-clean", "lint-fails"])
def test_scoped_no_tests_story_is_graded_on_lint_only(
    plan_dir, monkeypatch, lint_returncode, expected_status,
):
    _setup(plan_dir, monkeypatch, lint_returncode=lint_returncode,
           story_extra={"test_scope_resolution": {"test_scope": "scoped"}})

    result = p.check_story_status("tsc", "S1")

    assert result["status"] == expected_status


def test_scoped_non_pytest_runner_without_acceptance_grades_no_tests_cmd(plan_dir, monkeypatch):
    _, test_runs = _setup(plan_dir, monkeypatch, detected=["make", "test"],
                          story_extra={"test_scope_resolution": {"test_scope": "scoped"}})

    p.check_story_status("tsc", "S1")

    assert test_runs[0] == tsr.NO_TESTS_CMD


# ---------- Scoped: ci_rework failing node ids ----------

def test_scoped_ci_rework_grades_failing_node_id(plan_dir, monkeypatch):
    worktree, test_runs = _setup(
        plan_dir, monkeypatch,
        story_extra={"backend": "local", "ci_rework": True,
                     "review_feedback": CI_FEEDBACK,
                     "test_scope_resolution": {"test_scope": "scoped"}},
    )

    p.check_story_status("tsc", "S1")

    assert test_runs[0] == [
        "pytest", "-q", str(worktree / "tests/unit/test_a.py::test_x"),
    ]


def test_scoped_non_ci_rework_ignores_failing_node_ids(plan_dir, monkeypatch):
    _, test_runs = _setup(
        plan_dir, monkeypatch,
        story_extra={"backend": "local", "ci_rework": False,
                     "review_feedback": CI_FEEDBACK,
                     "test_scope_resolution": {"test_scope": "scoped"}},
    )

    p.check_story_status("tsc", "S1")

    assert test_runs[0] == tsr.NO_TESTS_CMD


# ---------- Wiring, size and docs pins ----------

def test_grade_helper_is_exported_onto_server():
    assert p.grade_test_cmd is tsr.grade_test_cmd


def test_story_status_stays_within_line_cap():
    line_count = len(Path(story_status.__file__).read_text().splitlines())
    assert_within_line_limit(line_count, 999, "pipeline/story_status.py")


def test_reference_documents_no_tests_record():
    reference = (REPO_ROOT / "REFERENCE.md").read_text()
    assert "`test_scope_grade.no_tests` is recorded on the story" in reference
