"""The done-bar gate must never be narrower than the full test suite.

Regression guard for the defect that let PR #552 merge red and broke
master on 2026-09-02.

`check_story_status` appends a story's own added `tests/**/test_*.py`
files to the detected pytest command (`_added_pytest_test_paths`, the
Mode 42 fix). Passing a positional path to pytest REPLACES its default
collection rather than adding to it, so that append silently converts the
full-suite done-bar into a single-file done-bar:

    pytest --override-ini=testpaths=. --ignore=tests/benchmark        -> 3 tests
    pytest --override-ini=testpaths=. --ignore=tests/benchmark A.py   -> 2 tests

The Mode 42 append was only ever needed because the detected command used
to carry a blanket `--ignore=tests`. That blanket ignore was removed
(`_apply_pytest_collection_overrides` now emits only
`--ignore=tests/benchmark --ignore=tests/experiments`), so for a story
adding `tests/unit/test_x.py` the path is ALREADY collected and appending
it can only ever subtract from the gate.

Live consequence: story f68b8350's `last_test_check.cmd` ended in
`.../tests/unit/test_guard_liveness_check.py`, graded 23 tests from the
story's own new file, never ran the pre-existing sibling
`test_guard_liveness_parse.py` that was red, returned 0, and the story
merged as APPROVE onto a broken master.

These tests are written FIRST (TDD) and are RED until
`_pytest_ignored_paths` / `_is_hidden_by_pytest_ignores` exist in
pipeline.build_detect and check_story_status consults them.
"""

import json
import subprocess

import pytest

from pipeline import server as p

# --------------------------------------------------------------------------
# Unit: which paths does a pytest command's --ignore flags actually hide?
# --------------------------------------------------------------------------


def test_ignored_paths_parses_equals_form():
    from pipeline.build_detect import _pytest_ignored_paths
    cmd = ["pytest", "-q", "--ignore=tests/benchmark", "--ignore=tests/experiments"]
    assert _pytest_ignored_paths(cmd) == ["tests/benchmark", "tests/experiments"]


def test_ignored_paths_parses_separate_arg_form():
    from pipeline.build_detect import _pytest_ignored_paths
    cmd = ["pytest", "--ignore", "tests/benchmark", "-q"]
    assert _pytest_ignored_paths(cmd) == ["tests/benchmark"]


def test_ignored_paths_empty_when_no_ignore_flags():
    from pipeline.build_detect import _pytest_ignored_paths
    assert _pytest_ignored_paths(["pytest", "-q"]) == []


def test_ignored_paths_ignores_trailing_bare_flag():
    """A dangling `--ignore` with no value must not IndexError."""
    from pipeline.build_detect import _pytest_ignored_paths
    assert _pytest_ignored_paths(["pytest", "--ignore"]) == []


@pytest.mark.parametrize("path", [
    pytest.param("tests/benchmark/test_x.py", id="direct-child-of-bench-dir"),
    pytest.param("tests/benchmark/deep/nested/test_y.py", id="nested-under-bench-dir"),
    pytest.param("tests/experiments/test_z.py", id="direct-child-of-exp-dir"),
])
def test_path_under_ignored_dir_is_hidden(path):
    from pipeline.build_detect import _is_hidden_by_pytest_ignores
    assert _is_hidden_by_pytest_ignores(
        path, ["tests/benchmark", "tests/experiments"]
    ) is True


@pytest.mark.parametrize("path", [
    pytest.param("tests/unit/test_x.py", id="unit-dir-not-ignored"),
    pytest.param("tests/test_top_level.py", id="tests-root-not-ignored"),
    # prefix-similar to the ignored "tests/benchmark" dir, but NOT under it -
    # id deliberately avoids the literal substring "tests/benchmark" so this
    # file's own collection output can't trip
    # test_pytest_collection_allowlist.py's bare-pytest substring check.
    pytest.param("tests/benchmarking/test_notmatching.py", id="prefix-similar-not-under-it"),
])
def test_path_outside_ignored_dirs_is_not_hidden(path):
    from pipeline.build_detect import _is_hidden_by_pytest_ignores
    assert _is_hidden_by_pytest_ignores(
        path, ["tests/benchmark", "tests/experiments"]
    ) is False


def test_no_ignores_hides_nothing():
    from pipeline.build_detect import _is_hidden_by_pytest_ignores
    assert _is_hidden_by_pytest_ignores("tests/unit/test_x.py", []) is False


# --------------------------------------------------------------------------
# Integration: the recorded done-bar command must not be narrowed
# --------------------------------------------------------------------------

BASE_CMD = ["pytest", "--override-ini=testpaths=.",
            "--ignore=tests/benchmark", "--ignore=tests/experiments"]


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _setup(plan_dir, monkeypatch, *, added_paths, acceptance=None,
           test_cmd=None):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    story = {"summary": "thing", "status": "in_progress",
             "pid": 4242, "worktree": str(worktree)}
    if acceptance:
        story["acceptance"] = acceptance
    (plan_dir / "dn.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2)
    )
    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        p, "detect_test_command", lambda wt: (wt, list(test_cmd or BASE_CMD)),
    )
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: None)
    monkeypatch.setattr(
        p, "_added_pytest_test_paths", lambda *a, **k: list(added_paths),
    )
    monkeypatch.setattr(p, "_find_dead_new_functions", lambda *a, **k: [])

    def run_mock(cmd, **kwargs):
        if list(cmd)[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="10 passed", stderr="")

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return worktree


def _recorded_cmd(plan_dir):
    manifest = json.loads((plan_dir / "dn.manifest.json").read_text())
    return manifest["stories"]["S1"]["last_test_check"]["cmd"]


def test_collected_test_file_is_not_appended(plan_dir, monkeypatch):
    """THE REGRESSION. A story adding tests/unit/test_x.py must leave the
    full-suite command untouched - that path is already collected, so
    appending it would narrow the gate to just that file."""
    _setup(plan_dir, monkeypatch, added_paths=["tests/unit/test_x.py"])

    p.check_story_status("dn", "S1")

    assert _recorded_cmd(plan_dir) == BASE_CMD


def test_guard_liveness_shape_is_not_narrowed(plan_dir, monkeypatch):
    """The exact PR #552 shape: the recorded command must not end in the
    story's own new test file."""
    _setup(plan_dir, monkeypatch,
           added_paths=["tests/unit/test_guard_liveness_check.py"])

    p.check_story_status("dn", "S1")

    cmd = _recorded_cmd(plan_dir)
    assert not any(c.endswith("test_guard_liveness_check.py") for c in cmd), (
        f"done-bar narrowed to the story's own test file: {cmd}"
    )


def test_hidden_test_file_is_still_appended(plan_dir, monkeypatch):
    """Mode 42 preserved: a path the command genuinely hides
    (tests/benchmark/, excluded by --ignore) must still be passed
    explicitly, or the story's own deliverable never runs."""
    _setup(plan_dir, monkeypatch,
           added_paths=["tests/benchmark/test_real_repo.py"])

    p.check_story_status("dn", "S1")

    cmd = _recorded_cmd(plan_dir)
    assert any(c.endswith("tests/benchmark/test_real_repo.py") for c in cmd), (
        f"hidden test path was dropped from the gate: {cmd}"
    )


def test_mixed_paths_append_only_the_hidden_one(plan_dir, monkeypatch):
    """Boundary: given one collected and one hidden path, only the hidden
    one is appended."""
    _setup(plan_dir, monkeypatch, added_paths=[
        "tests/unit/test_collected.py",
        "tests/experiments/test_hidden.py",
    ])

    p.check_story_status("dn", "S1")

    cmd = _recorded_cmd(plan_dir)
    assert not any(c.endswith("test_collected.py") for c in cmd)
    assert any(c.endswith("tests/experiments/test_hidden.py") for c in cmd)


def test_no_added_paths_leaves_command_untouched(plan_dir, monkeypatch):
    """Boundary: empty added-path list changes nothing."""
    _setup(plan_dir, monkeypatch, added_paths=[])

    p.check_story_status("dn", "S1")

    assert _recorded_cmd(plan_dir) == BASE_CMD


def test_non_pytest_runner_is_untouched(plan_dir, monkeypatch):
    """Negative: a cargo command must not gain pytest path arguments."""
    cargo = ["cargo", "test"]
    _setup(plan_dir, monkeypatch, added_paths=["tests/unit/test_x.py"],
           test_cmd=cargo)

    p.check_story_status("dn", "S1")

    assert _recorded_cmd(plan_dir) == cargo


def test_acceptance_story_still_scoped_to_oracle(plan_dir, monkeypatch):
    """Negative: a story WITH an acceptance block takes the FM-A oracle
    path, so the added-paths logic must not run at all for it."""
    _setup(plan_dir, monkeypatch,
           added_paths=["tests/unit/test_x.py"],
           acceptance=[{"path": "tests/acceptance_foo.py", "source": "x"}])

    p.check_story_status("dn", "S1")

    cmd = _recorded_cmd(plan_dir)
    assert not any(c.endswith("tests/unit/test_x.py") for c in cmd)
