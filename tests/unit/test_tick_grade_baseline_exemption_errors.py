"""Regression tests: a pytest ``ERROR`` line must block the baseline exemption.

Review feedback on TGE-1 (REQUEST_CHANGES, blocking):

``pipeline/story_status.py``'s ``_baseline_exempted_failures`` compares only the
node ids parsed from pytest's ``FAILED `` short-summary lines (via
``pipeline.build_detect.failed_node_ids``).  pytest reports collection errors
and fixture/setup/teardown errors as ``ERROR <nodeid>`` lines, which that
parser never sees.  A run that reports a baseline ``FAILED`` node PLUS a
brand-new ``ERROR`` therefore yields ``run_ids == [baseline_node]``, a subset
of ``baseline_ids`` -> exempted -> ``passed=True`` -> ``tests_passed``, even
though the agent just broke a test file or a fixture.  The baseline can never
itself contain an ERROR (it records only FAILED ids, and an all-error baseline
yields an empty ``failed_node_ids`` -> no exemption), so every ERROR in a run
is necessarily new and must block the exemption.  That contradicts the
helper's own documented fail-closed contract ("a run that reports a single
failure the baseline did not has to keep rejecting").

Two further holes in the same parser are covered here:

* ``line.split()[1]`` raises ``IndexError`` on a bare ``"FAILED "`` line, and
  ``_baseline_exempted_failures`` is called unguarded from
  ``check_story_status``, so that aborts the tick instead of failing closed.
* splitting on whitespace truncates parametrized node ids containing spaces
  (``tests/a.py::test_x[foo bar]``), so a genuinely new
  ``tests/a.py::test_x[foo baz]`` parses to the same id and is falsely
  exempted.

Written FIRST (TDD): every test below is RED against the current
implementation.  The integration tests drive the REAL ``check_story_status``
verdict path (``_baseline_exempted_failures`` is deliberately NOT patched), so
they also pin the manifest state the tick leaves behind for the next tick.
"""

import json
import subprocess

import pytest

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
from pipeline import build_detect as bd
from pipeline import server as p
from pipeline import story_status as ss

PLAN_NAME = "tge1err"
STORY_KEY = "S1"

BASELINE_NODE = "tests/a.py::test_x"
ERROR_NODE = "tests/b.py::test_y"

# pytest's short summary: FAILED lines carry a " - <reason>" suffix, ERROR
# lines may or may not (a bare collection error has no reason).
RED_STDOUT_BASELINE_ONLY = f"FAILED {BASELINE_NODE} - assert False\n"
RED_STDOUT_BASELINE_PLUS_ERROR = (
    f"FAILED {BASELINE_NODE} - assert False\n"
    f"ERROR {ERROR_NODE}\n"
)
RED_STDOUT_BASELINE_PLUS_ERROR_WITH_REASON = (
    f"FAILED {BASELINE_NODE} - assert False\n"
    f"ERROR {ERROR_NODE} - ImportError: no module named 'x'\n"
)


def _completed(stdout, returncode=1):
    return subprocess.CompletedProcess(
        ["pytest", "-q"], returncode, stdout=stdout, stderr=""
    )


def _baseline(returncode=1, node_ids=(BASELINE_NODE,)):
    """A baseline record as written by a post-change dispatch."""
    return {"returncode": returncode, "failed_node_ids": list(node_ids)}


# ---------------------------------------------------------------------------
# 1. The verdict helper: a new ERROR must block the exemption
# ---------------------------------------------------------------------------
def test_should_refuse_when_the_run_adds_an_error_the_baseline_lacked():
    """The blocking case: the run still reports the baseline FAILED node, but
    it ALSO reports an ERROR node the baseline cannot have had.  The combined
    failure+error set is not a subset of the baseline, so no exemption."""
    story = {"baseline_test_check": _baseline()}
    result = _completed(RED_STDOUT_BASELINE_PLUS_ERROR)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_run_adds_an_error_with_a_reason_suffix():
    """Same, with pytest's ``ERROR <nodeid> - <reason>`` shape."""
    story = {"baseline_test_check": _baseline()}
    result = _completed(RED_STDOUT_BASELINE_PLUS_ERROR_WITH_REASON)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_run_reports_only_an_error():
    """An all-error run (e.g. the agent broke an import in a test file) is a
    red run: the baseline records only FAILED ids, so this ERROR is new."""
    story = {"baseline_test_check": _baseline()}
    result = _completed(f"ERROR {ERROR_NODE}\n")
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_still_exempt_a_run_whose_failures_are_all_in_the_baseline():
    """Guard: handling ERROR lines must not break the exemption it exists
    for -- a run reporting only baseline failures is still exempted."""
    story = {"baseline_test_check": _baseline()}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) == [BASELINE_NODE]


def test_should_refuse_a_new_parametrized_id_that_truncates_to_a_baseline_id():
    """The baseline is recorded by the same parser, so a space-containing
    parametrized id was stored truncated; a DIFFERENT parametrization must not
    compare equal to it and be waved through."""
    story = {
        "baseline_test_check": _baseline(node_ids=("tests/a.py::test_x[foo",))
    }
    result = _completed("FAILED tests/a.py::test_x[foo baz] - assert False\n")
    assert ss._baseline_exempted_failures(story, result) is None


# ---------------------------------------------------------------------------
# 2. The parser: bare lines and space-containing node ids
# ---------------------------------------------------------------------------
def test_should_not_raise_on_a_bare_failed_line():
    """``line.split()[1]`` on ``"FAILED "`` raises IndexError; a line with no
    node id carries no failure and must be skipped."""
    assert bd.failed_node_ids("FAILED \n") == []


def test_should_skip_a_bare_failed_line_and_still_parse_the_real_one():
    stdout = f"FAILED \nFAILED {BASELINE_NODE} - assert False\n"
    assert bd.failed_node_ids(stdout) == [BASELINE_NODE]


def test_should_keep_a_parametrized_node_id_containing_spaces_whole():
    """Split on the `` - `` reason separator, not on whitespace, or the id is
    truncated at the first space."""
    stdout = "FAILED tests/a.py::test_x[foo bar] - assert False\n"
    assert bd.failed_node_ids(stdout) == ["tests/a.py::test_x[foo bar]"]


def test_should_still_parse_a_plain_node_id():
    assert bd.failed_node_ids(RED_STDOUT_BASELINE_ONLY) == [BASELINE_NODE]


# ---------------------------------------------------------------------------
# 3. Integration: the REAL check_story_status verdict path
# ---------------------------------------------------------------------------
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN_NAME}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir):
    manifest = json.loads((plan_dir / f"{PLAN_NAME}.manifest.json").read_text())
    return manifest["stories"][STORY_KEY]


def _dead_kill(pid, sig):
    raise ProcessLookupError(f"pid {pid} is gone")


def _install_grade_harness(
    plan_dir, monkeypatch, *, test_stdout, test_returncode=1, baseline=None
):
    """Mirror of tests/unit/test_tick_grade_baseline_exemption.py's harness: a
    worktree + manifest story whose pid is dead, test detection stubbed, and
    ``subprocess.run`` routed so the test command returns the given red result.
    The lint gate and the dead-function check are stubbed because they grade
    the story's own diff, not the baseline."""
    # The opt-in acceptance-fail review route would rewrite a genuine failure
    # to "tests_passed"; pin it off so the verdict is deterministic.
    monkeypatch.delenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", raising=False)

    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
    }
    if baseline is not None:
        story["baseline_test_check"] = baseline
    _write_manifest(plan_dir, {STORY_KEY: story})

    cmd = ["pytest", "-q"]
    monkeypatch.setattr(p.os, "kill", _dead_kill)
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, cmd))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_run_lint_gate", lambda *a, **k: None)
    monkeypatch.setattr(p, "_find_dead_new_functions", lambda *a, **k: [])

    def run_mock(cmd_, **kwargs):
        if list(cmd_)[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                cmd_, 0, stdout="deadbeef\n", stderr=""
            )
        return subprocess.CompletedProcess(
            cmd_, test_returncode, stdout=test_stdout, stderr=""
        )

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return {"worktree": worktree}


def test_should_reject_a_run_that_adds_an_error_to_a_baseline_failure(
    plan_dir, monkeypatch
):
    """The wiring is what is graded here, not just the helper: the REAL
    ``check_story_status`` must not wave through a run that adds an ERROR."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_BASELINE_PLUS_ERROR,
        baseline=_baseline(),
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)
    story = _read_story(plan_dir)

    assert result["tests_passed"] is False
    assert result["status"] != "tests_passed"
    assert story["status"] != "tests_passed"
    assert "grade_baseline_exemption" not in story


def test_the_manifest_left_by_the_tick_keeps_the_story_blocked(
    plan_dir, monkeypatch
):
    """Tick 2 reads the manifest Tick 1 wrote: the broken suite must stay
    blocked there, not just in Tick 1's return value."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_BASELINE_PLUS_ERROR,
        baseline=_baseline(),
    )

    p.check_story_status(PLAN_NAME, STORY_KEY)

    persisted = json.loads(
        (plan_dir / f"{PLAN_NAME}.manifest.json").read_text()
    )
    story = persisted["stories"][STORY_KEY]
    assert story["status"] != "tests_passed"
    assert story.get("tests_passed") is not True
    assert "grade_baseline_exemption" not in story


def test_a_bare_failed_line_does_not_abort_the_tick(plan_dir, monkeypatch):
    """A bare ``"FAILED "`` line (no node id) must be skipped, not raise
    IndexError out of ``check_story_status`` and abort the tick."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=f"FAILED {BASELINE_NODE} - assert False\nFAILED \n",
        baseline=_baseline(),
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)

    assert isinstance(result, dict)
    assert "status" in result
