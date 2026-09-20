"""Tick-side grade must not reject a story for failures that predate it.

``pipeline/dispatch.py``'s ``_run_baseline_test_snapshot`` already runs the
detected test command ONCE against a story's freshly created, still-unmodified
worktree on its first dispatch and records ``story["baseline_test_check"]``
when that command ALREADY failed.  That record can therefore only ever
describe failures that predate the story's own edits -- yet the tick-side
grade in ``pipeline/story_status.py`` still decided with a bare
``passed = test_result.returncode == 0``, so a story dispatched onto an
already-red suite was rejected for a failure it did not cause (a reproducible
pre-existing failure, not a flake -- no retry helps).

This story makes the grade consult that baseline: a red run whose failing
node ids are all in the recorded baseline is exempted (and the exemption is
recorded on the manifest for audit), while a run that adds even one failure
the baseline did not have keeps rejecting.  Flaky/transient runs are a
sibling story and are deliberately NOT covered here.

Written FIRST (TDD): every test below is RED against the current
implementation -- ``AttributeError`` on ``pipeline.build_detect.failed_node_ids``
and on ``pipeline.story_status._baseline_exempted_failures``, and a plain
assertion failure for the integration tests, which drive the REAL
``check_story_status`` verdict path.  The new names are reached by attribute
access at runtime (never a module-level ``from ... import``) so collection
still succeeds before they exist.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
# Every existing story_status test file does the same.
from pipeline import build_detect as bd
from pipeline import dispatch
from pipeline import server as p
from pipeline import story_status as ss

PLAN_NAME = "tge1"
STORY_KEY = "S1"

BASELINE_NODE = "tests/a.py::test_x"
NEW_NODE = "tests/b.py::test_y"

# pytest's short summary line per failure; the node id is the second token.
RED_STDOUT_BASELINE_ONLY = f"FAILED {BASELINE_NODE} - assert False\n"
RED_STDOUT_WITH_NEW_FAILURE = (
    f"FAILED {BASELINE_NODE} - assert False\n"
    f"FAILED {NEW_NODE} - assert False\n"
)


def _completed(stdout, returncode=1):
    return subprocess.CompletedProcess(
        ["pytest", "-q"], returncode, stdout=stdout, stderr=""
    )


def _baseline(returncode=1, node_ids=(BASELINE_NODE,)):
    """A baseline record as written by a post-change dispatch."""
    return {"returncode": returncode, "failed_node_ids": list(node_ids)}


# ---------------------------------------------------------------------------
# 1. The parser: pipeline.build_detect.failed_node_ids
# ---------------------------------------------------------------------------
def test_should_parse_the_node_id_from_a_failed_summary_line():
    """The node id is the second whitespace-separated token of a line that
    starts with ``FAILED `` (trailing space)."""
    assert bd.failed_node_ids(RED_STDOUT_BASELINE_ONLY) == [BASELINE_NODE]


def test_should_sort_and_dedupe_parsed_node_ids():
    """The parser's contract is a SORTED, DE-DUPLICATED list, so the
    comparison against the baseline never depends on runner output order."""
    stdout = (
        f"FAILED {NEW_NODE} - assert False\n"
        f"FAILED {BASELINE_NODE} - assert False\n"
        f"FAILED {NEW_NODE} - assert False\n"
    )
    assert bd.failed_node_ids(stdout) == [BASELINE_NODE, NEW_NODE]


def test_should_return_empty_for_output_without_failed_lines():
    """A non-pytest runner prints no ``FAILED `` summary lines; callers must
    read that as "no parseable failures", never as "nothing failed"."""
    assert bd.failed_node_ids("1 passed in 0.01s\n") == []


def test_should_return_empty_for_empty_input():
    assert bd.failed_node_ids("") == []


def test_should_return_empty_for_none_input():
    """The argument is ``str | None`` and is defended with ``(stdout or "")``."""
    assert bd.failed_node_ids(None) == []


def test_should_ignore_lines_that_merely_contain_failed():
    """Only a line STARTING with ``FAILED `` counts -- a bare mention of the
    word (or a missing trailing space) is not a failure summary line."""
    assert bd.failed_node_ids("see FAILED tests/a.py::test_x above\n") == []


def test_the_parser_sits_above_detect_test_command():
    """Placement requirement: the single parser both call sites use lives
    immediately above ``detect_test_command`` in the same module."""
    src = Path(bd.__file__).read_text()
    assert "def failed_node_ids(" in src
    assert src.index("def failed_node_ids(") < src.index("def detect_test_command(")


# ---------------------------------------------------------------------------
# 2. The verdict helper: pipeline.story_status._baseline_exempted_failures
# ---------------------------------------------------------------------------
def test_should_exempt_a_failure_the_baseline_was_already_failing():
    story = {"baseline_test_check": _baseline()}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) == [BASELINE_NODE]


def test_should_exempt_when_the_run_is_a_strict_subset_of_the_baseline():
    """The run may report fewer failures than the baseline (the agent fixed
    one of them); a subset is still fully covered by the baseline."""
    story = {"baseline_test_check": _baseline(node_ids=(BASELINE_NODE, NEW_NODE))}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) == [BASELINE_NODE]


def test_should_refuse_when_the_run_adds_a_failure_the_baseline_lacked():
    """Fail-closed: one failure the baseline did not have keeps the run red."""
    story = {"baseline_test_check": _baseline()}
    result = _completed(RED_STDOUT_WITH_NEW_FAILURE)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_without_a_recorded_baseline():
    assert ss._baseline_exempted_failures({}, _completed(RED_STDOUT_BASELINE_ONLY)) is None


def test_should_refuse_when_the_baseline_was_passing():
    """A baseline that itself passed is no justification for a red run."""
    story = {"baseline_test_check": _baseline(returncode=0)}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_baseline_returncode_is_unknown():
    """``returncode`` of None is not a recorded failure either."""
    story = {"baseline_test_check": _baseline(returncode=None)}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_baseline_has_no_parsed_failures():
    """A failing baseline with no parseable node ids (non-pytest runner, or a
    truncated payload) leaves nothing to compare against."""
    story = {"baseline_test_check": _baseline(node_ids=())}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_run_reports_no_parseable_failure():
    """An unparseable red run is a red run."""
    story = {"baseline_test_check": _baseline()}
    result = _completed("1 failed in 0.01s\n")
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_a_baseline_recorded_before_the_parser_existed():
    """A baseline dict written by an earlier dispatch has no
    ``failed_node_ids`` key at all: it must fall back to the failing path,
    never to an exemption -- and never raise KeyError."""
    story = {
        "baseline_test_check": {
            "returncode": 1,
            "stdout_tail": RED_STDOUT_BASELINE_ONLY,
            "stderr_tail": "",
        }
    }
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_baseline_record_is_not_a_dict():
    story = {"baseline_test_check": "1 failed"}
    result = _completed(RED_STDOUT_BASELINE_ONLY)
    assert ss._baseline_exempted_failures(story, result) is None


def test_should_refuse_when_the_test_result_has_no_stdout_attribute():
    """Some test doubles for subprocess.run's return value do not define
    ``stdout``; the helper reads it with getattr and must not raise."""

    class _NoStdout:
        returncode = 1

    story = {"baseline_test_check": _baseline()}
    assert ss._baseline_exempted_failures(story, _NoStdout()) is None


# ---------------------------------------------------------------------------
# 3. Wiring: the helper must be reachable from the rebound grade body
# ---------------------------------------------------------------------------
def test_story_status_imports_the_parser_from_build_detect():
    """Edit 3a: the parser is imported into story_status's namespace."""
    assert ss.failed_node_ids is bd.failed_node_ids


def test_the_helper_is_exported_into_the_server_namespace():
    """Edit 3c: the grade body's globals are REBOUND to pipeline.server's
    namespace, so the bare name it calls has to be reachable there or every
    red-path grade dies with NameError."""
    assert p._baseline_exempted_failures is ss._baseline_exempted_failures


def test_the_export_line_precedes_the_pytest_guard():
    """The export is deliberately NOT inside the ``if "pytest" not in
    sys.modules:`` block, so the grading tests exercise the real verdict path
    rather than a name that only exists in production. The guard keeps its two
    detached-grade primitives."""
    src = Path(ss.__file__).read_text()
    export = "_server._baseline_exempted_failures = _baseline_exempted_failures"
    guard = 'if "pytest" not in sys.modules:'
    assert export in src
    assert guard in src
    assert src.index(export) < src.index(guard)
    # The two detached-grade primitives stay attached to the guard.
    assert src.index(guard) < src.index(
        "_server.start_detached_grade = start_detached_grade"
    )
    assert src.index(guard) < src.index(
        "_server.collect_detached_grade = collect_detached_grade"
    )


def test_the_helper_is_module_level_and_above_the_grade_wrapper():
    """Edit 3b: module level (not nested in a function), immediately above
    the GRADE_WRAPPER assignment."""
    src = Path(ss.__file__).read_text()
    assert "\ndef _baseline_exempted_failures(" in src
    assert src.index("def _baseline_exempted_failures(") < src.index("GRADE_WRAPPER = ")


def test_the_grade_body_calls_the_helper():
    """Edit 3d: the verdict consults the helper instead of trusting the bare
    returncode alone."""
    src = Path(ss.__file__).read_text()
    assert "passed = test_result.returncode == 0" in src
    assert "_baseline_exempted_failures(story, test_result)" in src


def test_dispatch_imports_the_parser_rather_than_redefining_it():
    """Edit 2: the snapshot's call site imports the single parser from
    build_detect; there is no second copy of it in dispatch.py."""
    src = Path(dispatch.__file__).read_text()
    assert "from .build_detect import detect_test_command, failed_node_ids" in src
    assert "def failed_node_ids(" not in src


def test_story_status_does_not_redefine_the_parser():
    """Edit 3a: story_status imports the parser too -- one implementation,
    two call sites."""
    src = Path(ss.__file__).read_text()
    assert "def failed_node_ids(" not in src


# ---------------------------------------------------------------------------
# 4. Integration: the REAL check_story_status verdict path
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
    """Mirror tests/unit/test_check_story_status_lint_gate.py's ``_base_setup``:
    a worktree + manifest story whose pid is dead, test detection stubbed, and
    ``subprocess.run`` routed so the test command returns the given red/green
    result.  The lint gate and the dead-function check are stubbed (recording
    their calls) because they grade the story's own diff, not the baseline.
    """
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

    calls = []
    cmd = ["pytest", "-q"]
    monkeypatch.setattr(p.os, "kill", _dead_kill)
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, cmd))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    def lint_spy(*a, **k):
        calls.append("lint")

    def dead_code_spy(*a, **k):
        calls.append("dead_code")
        return []

    monkeypatch.setattr(p, "_run_lint_gate", lint_spy)
    monkeypatch.setattr(p, "_find_dead_new_functions", dead_code_spy)

    def run_mock(cmd_, **kwargs):
        if list(cmd_)[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                cmd_, 0, stdout="deadbeef\n", stderr=""
            )
        return subprocess.CompletedProcess(
            cmd_, test_returncode, stdout=test_stdout, stderr=""
        )

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return {"worktree": worktree, "calls": calls}


def test_harness_reaches_the_grade_and_passes_a_green_run(plan_dir, monkeypatch):
    """Sanity guard for the two integration tests below: this harness really
    does drive the verdict path (a green run lands on ``tests_passed``)."""
    _install_grade_harness(
        plan_dir, monkeypatch, test_stdout="1 passed\n", test_returncode=0
    )
    assert p.check_story_status(PLAN_NAME, STORY_KEY)["status"] == "tests_passed"


def test_should_pass_a_story_whose_only_failures_are_in_its_baseline(
    plan_dir, monkeypatch
):
    """The wiring is what is graded here, not just the helper: the REAL
    ``check_story_status`` must consult the recorded baseline and exempt a red
    run whose failures it already had -- recording exactly what it waved
    through.  ``_baseline_exempted_failures`` is deliberately NOT patched, so
    this exercises the exported real one."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_BASELINE_ONLY,
        baseline=_baseline(),
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)
    story = _read_story(plan_dir)

    assert result["status"] == "tests_passed"
    assert story["status"] == "tests_passed"
    assert story["grade_baseline_exemption"]["failed_node_ids"] == [BASELINE_NODE]
    assert story["grade_baseline_exemption"]["baseline_returncode"] == 1
    assert story["grade_baseline_exemption"]["ts"]


def test_should_reject_a_story_whose_run_adds_a_new_failure(plan_dir, monkeypatch):
    """Fail-closed end to end: one failure the baseline did not have keeps the
    story rejected, and nothing is recorded as exempted."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_WITH_NEW_FAILURE,
        baseline=_baseline(),
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)
    story = _read_story(plan_dir)

    assert result["status"] != "tests_passed"
    assert story["status"] != "tests_passed"
    assert "grade_baseline_exemption" not in story


def test_should_reject_a_story_whose_baseline_predates_the_parser(
    plan_dir, monkeypatch
):
    """A baseline dict from a pre-change dispatch (no ``failed_node_ids`` key)
    must reject, not crash."""
    _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_BASELINE_ONLY,
        baseline={"returncode": 1, "stdout_tail": RED_STDOUT_BASELINE_ONLY},
    )

    result = p.check_story_status(PLAN_NAME, STORY_KEY)
    story = _read_story(plan_dir)

    assert result["status"] != "tests_passed"
    assert "grade_baseline_exemption" not in story


def test_should_still_run_the_lint_and_dead_code_checks_for_an_exempted_story(
    plan_dir, monkeypatch
):
    """The exemption only clears the test verdict: the lint gate and the
    dead-function check that follow still run, because they grade the story's
    own diff, which the baseline says nothing about."""
    harness = _install_grade_harness(
        plan_dir,
        monkeypatch,
        test_stdout=RED_STDOUT_BASELINE_ONLY,
        baseline=_baseline(),
    )

    p.check_story_status(PLAN_NAME, STORY_KEY)

    assert harness["calls"] == ["lint", "dead_code"]


# ---------------------------------------------------------------------------
# 5. The snapshot must carry the FULL stdout's failures, not the tail
# ---------------------------------------------------------------------------
def test_snapshot_parses_failures_outside_the_truncated_tail(tmp_path, monkeypatch):
    """``failed_node_ids`` is parsed from the FULL ``r.stdout``: a failure
    summary that falls outside the 2000-char ``stdout_tail`` must still be
    recorded, or the tick-side comparison would silently weaken."""
    wt = tmp_path / "wt"
    wt.mkdir()
    filler = "x" * 3000
    script = (
        "import sys;"
        "print('FAILED tests/a.py::test_x - assert False');"
        f"print('{filler}');"
        "sys.exit(1)"
    )
    cmd = [sys.executable, "-c", script]
    monkeypatch.setattr(
        "pipeline.build_detect.detect_test_command", lambda path: (str(wt), cmd)
    )

    result = dispatch._run_baseline_test_snapshot(wt)

    assert result["failed_node_ids"] == ["tests/a.py::test_x"]
