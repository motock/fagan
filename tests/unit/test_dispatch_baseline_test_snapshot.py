"""Tests for the pre-dispatch baseline test snapshot (issue 6166c2f6).

On 2026-09-17 at least 4 independent dispatched stories each independently
rediscovered the same pre-existing, out-of-scope test failures (a "5
pre-existing ... failures (httpx missing in subprocess interpreter)" pattern,
named almost verbatim in 4 separate journal entries across 2 different plans)
and each spent part of its own step/rework budget investigating or trying to
fix them before concluding they were unrelated to its assigned task.

The fix: on a story's FIRST (non-resuming) dispatch, run the detected test
command ONCE against the freshly created, still-unmodified worktree and
remember whether it already failed. When it did, prepend a short note to the
executor prompt telling the agent the failure predates its own changes.

Written TDD-first: every test below fails (``AttributeError`` on
``pipeline.dispatch._run_baseline_test_snapshot``, or a missing prompt note)
until the implementation lands. The helper is reached by attribute access at
runtime (never a module-level ``from ... import``) so collection still
succeeds before the function exists.
"""
import json
import subprocess
import sys

import pytest

from app import backend
from pipeline import dispatch
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._line_limit_policy import assert_within_line_limit


# ---------------------------------------------------------------------------
# Fixtures/helpers (copied from tests/unit/test_dispatch_staleness.py per this
# repo's convention - there is no shared conftest.py for these). Defined
# locally rather than imported from tests/unit/_always_on_planner_helpers.py:
# that module's importers are covered by a per-file-ignore for ruff's F811
# false positive (fixture names reused as test parameters) that this file is
# not, so importing the fixtures here would fail lint.
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import persistence as ppers
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")

NOTE = "NOTE: the test command already fails"
MARKER = ".dispatch_baseline_test_checked"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _CapturingDispatchBackend:
    """Stand-in for whatever backend.get_backend(...) returns, capturing the
    exact kwargs _dispatch_story_impl handed to dispatch()."""

    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeProc(4242)

    def complete(self, prompt, **kwargs):  # planner/diagnosis roles, if reached
        return ""


def _stub_fresh_dispatch(monkeypatch, worktree_path, *, preexisting_marker=None,
                         preexisting_marker_content=None):
    """Stub every external boundary a FRESH (non-resuming) local dispatch
    touches so it runs to completion without real git/gh/Plane/subprocess, and
    simulate ``git worktree add`` actually creating the worktree directory -
    optionally already carrying a marker file, exactly as a marker committed
    to the base branch would. ``preexisting_marker_content`` writes that
    file's bytes verbatim (a JSON marker payload); without it the marker is
    the bare ``ok`` sentinel. Returns the capturing dispatch backend."""
    def _fake_run(cmd, **kw):
        if (
            isinstance(cmd, (list, tuple))
            and list(cmd)[:3] == ["git", "worktree", "add"]
        ):
            worktree_path.mkdir(parents=True, exist_ok=True)
            if preexisting_marker is not None:
                if preexisting_marker_content is None:
                    content = "ok\n"
                else:
                    content = preexisting_marker_content
                (worktree_path / preexisting_marker).write_text(content)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        backend.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc(9500)
    )
    monkeypatch.setattr(
        pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_provision_worktree_venv", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)
    capturing = _CapturingDispatchBackend()
    monkeypatch.setattr(backend, "get_backend", lambda role, name=None: capturing)
    return capturing


def _story(plan_dir, plan_name, story_key):
    manifest = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())
    return manifest["stories"][story_key]


def _fresh_story(status="todo", **overrides):
    story = {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": status,
        "dependencies": [],
    }
    story.update(overrides)
    return story


# ---------------------------------------------------------------------------
# 1. _run_baseline_test_snapshot itself
# ---------------------------------------------------------------------------
def test_snapshot_returns_failing_returncode(tmp_path, monkeypatch):
    """A worktree whose detected test command exits non-zero must come back
    as a dict carrying that exact returncode, in the same shape
    story_status.py's last_test_check already uses."""
    wt = tmp_path / "wt"
    wt.mkdir()
    cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]
    monkeypatch.setattr(
        "pipeline.build_detect.detect_test_command", lambda path: (str(wt), cmd)
    )

    result = dispatch._run_baseline_test_snapshot(wt)

    assert result is not None
    assert result["returncode"] == 1
    assert result["cmd"] == cmd
    assert set(result) == {
        "cmd",
        "failed_node_ids",
        "returncode",
        "stdout_tail",
        "stderr_tail",
    }
    assert result["failed_node_ids"] == []


def test_snapshot_records_parsed_failing_node_ids(tmp_path, monkeypatch):
    """The snapshot payload carries the failing node ids the runner reported,
    parsed from the FULL stdout, so the tick-side grade can compare a later
    red run against exactly what was already failing before the story's own
    edits. A command that prints no pytest failure summary records []."""
    wt = tmp_path / "wt"
    wt.mkdir()
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys;"
            "print('FAILED tests/a.py::test_x - assert False');"
            "sys.exit(1)"
        ),
    ]
    monkeypatch.setattr(
        "pipeline.build_detect.detect_test_command", lambda path: (str(wt), cmd)
    )

    result = dispatch._run_baseline_test_snapshot(wt)

    assert result["failed_node_ids"] == ["tests/a.py::test_x"]


def test_snapshot_returns_zero_for_passing(tmp_path, monkeypatch):
    """A worktree whose tests already pass must report returncode 0 - the
    caller uses that to decide there is nothing worth telling the agent."""
    wt = tmp_path / "wt"
    wt.mkdir()
    cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    monkeypatch.setattr(
        "pipeline.build_detect.detect_test_command", lambda path: (str(wt), cmd)
    )

    result = dispatch._run_baseline_test_snapshot(wt)

    assert result is not None
    assert result["returncode"] == 0


def test_snapshot_returns_none_for_missing_worktree(tmp_path, monkeypatch):
    """A non-existent worktree path must return None without ever shelling
    out - this is an observability hook, never a gate, so it must not raise
    (or waste a subprocess) on a path that isn't there."""
    missing = tmp_path / "does-not-exist"

    def _boom(*a, **k):
        raise AssertionError("no subprocess may run for a missing worktree")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert dispatch._run_baseline_test_snapshot(missing) is None


# ---------------------------------------------------------------------------
# 2. The dispatch flow: snapshot recorded + note prepended
# ---------------------------------------------------------------------------
def test_fresh_dispatch_sets_baseline_and_note(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A fresh local dispatch against a worktree whose baseline test command
    fails must record the result on the story AND prepend the note to the
    executor prompt, so the agent is told up front."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselinefail", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    capturing = _stub_fresh_dispatch(monkeypatch, worktree_path)

    snapshot_calls = []

    def _fake_snapshot(path):
        snapshot_calls.append(path)
        return {
            "cmd": ["pytest"],
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _fake_snapshot)

    result = p.dispatch_story("baselinefail", "S1")

    assert result["ok"] is True
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0] == worktree_path
    assert _story(plan_dir, "baselinefail", "S1")["baseline_test_check"][
        "returncode"
    ] == 1
    prompt = capturing.calls[0]["prompt"]
    assert NOTE in prompt
    # The existing WORKTREE_SCOPE_RULE prepend must survive alongside it.
    assert "working directory" in prompt.lower()


def test_passing_baseline_sets_nothing(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Only a FAILING baseline is worth telling the agent about: a passing
    baseline must leave the story key unset and add no note."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselinepass", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    capturing = _stub_fresh_dispatch(monkeypatch, worktree_path)

    monkeypatch.setattr(
        dispatch, "_run_baseline_test_snapshot",
        lambda path: {
            "cmd": ["pytest"],
            "returncode": 0,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )

    result = p.dispatch_story("baselinepass", "S1")

    assert result["ok"] is True
    assert "baseline_test_check" not in _story(plan_dir, "baselinepass", "S1")
    assert NOTE not in capturing.calls[0]["prompt"]


def test_resume_skips_baseline(plan_dir, worktree_root, agents_dir, monkeypatch):
    """A RESUMED dispatch acts on a worktree the agent has already been
    editing, so there is no meaningful "before" state left to snapshot: the
    helper must never be called and no note added."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    _write_manifest(
        plan_dir, "baselineresume", {"S1": _fresh_story(status="interrupted")}
    )
    capturing = _stub_fresh_dispatch(monkeypatch, worktree_path)

    def _boom(path):
        raise AssertionError("baseline snapshot must not run on a resume")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)

    result = p.dispatch_story("baselineresume", "S1")

    assert result["ok"] is True
    assert "baseline_test_check" not in _story(plan_dir, "baselineresume", "S1")
    assert NOTE not in capturing.calls[0]["prompt"]


def test_marker_prevents_second_run(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An existing .dispatch_baseline_test_checked marker means the snapshot
    already ran for this worktree - a later fresh-looking call must not run
    it again (mirrors the .tdd_split_test_author_done marker pattern)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselinemarker", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    capturing = _stub_fresh_dispatch(
        monkeypatch, worktree_path, preexisting_marker=MARKER,
    )

    def _boom(path):
        raise AssertionError("the marker must prevent a second baseline run")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)

    result = p.dispatch_story("baselinemarker", "S1")

    assert result["ok"] is True
    assert "baseline_test_check" not in _story(plan_dir, "baselinemarker", "S1")
    assert NOTE not in capturing.calls[0]["prompt"]


def test_snapshot_exception_never_propagates(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The snapshot is an observability hook, never a gate: if it blows up,
    dispatch must proceed exactly as it would have with no baseline check at
    all - no story key, no note, and the marker still written so the failure
    is not retried on every subsequent dispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselineboom", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    capturing = _stub_fresh_dispatch(monkeypatch, worktree_path)

    def _boom(path):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)

    result = p.dispatch_story("baselineboom", "S1")

    assert result["ok"] is True
    assert "baseline_test_check" not in _story(plan_dir, "baselineboom", "S1")
    assert NOTE not in capturing.calls[0]["prompt"]
    assert (worktree_path / MARKER).exists()


# ---------------------------------------------------------------------------
# 3. The baseline must SURVIVE a redispatch (BASELINE-1, b3+b4 runs)
#
# ``story["baseline_test_check"]`` was lost five times across three paths in
# the b3+b4 runs (2026-11-09/10) and every loss needed a manual
# pause/edit/resume to restore the cached baseline:
#
#   * Path A - the recording was guarded on ``not baseline_marker.exists()``,
#     so a redispatch onto a worktree that ALREADY carried the marker
#     (startup-reconcile after a scheduler restart, or a rework) recorded
#     NOTHING even though the marker and the cached snapshot were both there.
#   * Path B - the dispatch runs with the plan lock released, so its manifest
#     write races a ``patch_story`` transaction read and the later
#     whole-manifest write persists a copy that PREDATES the baseline.
#   * Path C - a grade write does the same.
#
# All three ended in 15-16 unexempted environmental failures. The fix routes
# every concurrently-writable field through
# ``pipeline.story_fields.apply_story_fields`` and self-heals a redispatch
# that finds a marker but no recorded baseline.
# ---------------------------------------------------------------------------
class _FakeTestResult:
    """The slice of a test run ``_baseline_exempted_failures`` reads."""

    def __init__(self, stdout, returncode=1):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _exempted_failures(story, stdout):
    """The node ids the grade would exempt for ``story`` given this run."""
    from pipeline.detached_grade import _baseline_exempted_failures

    return _baseline_exempted_failures(story, _FakeTestResult(stdout))


def _marker_payload(worktree_path):
    """The marker file's JSON payload."""
    return json.loads((worktree_path / MARKER).read_text())


def _redispatch_with_marker(plan_dir, worktree_root, agents_dir, monkeypatch,
                            plan_name, marker_payload):
    """Dispatch onto a worktree that ALREADY carries the baseline marker.

    The marker means the snapshot already ran for this worktree, so the
    dispatch must not run it again - but it must still be able to RECORD the
    baseline it implies.
    """
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, plan_name, {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    capturing = _stub_fresh_dispatch(
        monkeypatch,
        worktree_path,
        preexisting_marker=MARKER,
        preexisting_marker_content=json.dumps(marker_payload),
    )

    def _boom(path):
        raise AssertionError("the marker must prevent a second baseline run")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)
    result = p.dispatch_story(plan_name, "S1")
    assert result["ok"] is True
    return worktree_path, capturing


def test_redispatch_with_marker_records_the_baseline_and_exempts_exact_ids(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Path A: a redispatch onto a worktree that already carries the marker
    records the failing baseline the marker carries (rc 1 plus the recorded
    node ids), and the grade then exempts EXACTLY those ids."""
    marker_ids = ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    _redispatch_with_marker(
        plan_dir,
        worktree_root,
        agents_dir,
        monkeypatch,
        "baselineheal",
        {"returncode": 1, "failed_node_ids": marker_ids},
    )

    story = _story(plan_dir, "baselineheal", "S1")
    baseline = story.get("baseline_test_check")
    assert baseline is not None, "the redispatch must record the baseline"
    assert baseline["returncode"] == 1
    assert baseline["failed_node_ids"] == marker_ids

    # The grade exempts exactly the ids the baseline was already failing.
    stdout = "".join(f"FAILED {node} - assert False\n" for node in marker_ids)
    assert _exempted_failures(story, stdout) == marker_ids


def test_redispatch_with_marker_does_not_exempt_a_new_failure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The exemption stays fail-closed: a run that ALSO fails on a node id the
    baseline did not is not exempted."""
    marker_ids = ["tests/test_a.py::test_one"]
    _redispatch_with_marker(
        plan_dir,
        worktree_root,
        agents_dir,
        monkeypatch,
        "baselineextra",
        {"returncode": 1, "failed_node_ids": marker_ids},
    )

    story = _story(plan_dir, "baselineextra", "S1")
    stdout = (
        "FAILED tests/test_a.py::test_one - assert False\n"
        "FAILED tests/test_c.py::test_three - assert False\n"
    )
    assert _exempted_failures(story, stdout) is None


def test_redispatch_with_empty_marker_ids_records_no_failing_baseline(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A marker that carries NO failing node ids implies nothing was failing,
    so no failing baseline may be recorded and nothing may be exempted."""
    _redispatch_with_marker(
        plan_dir,
        worktree_root,
        agents_dir,
        monkeypatch,
        "baselineempty",
        {"returncode": 1, "failed_node_ids": []},
    )

    story = _story(plan_dir, "baselineempty", "S1")
    baseline = story.get("baseline_test_check")
    if baseline is not None:
        # A recorded baseline must at least not claim a failure.
        assert baseline.get("returncode") in (None, 0)
        assert not baseline.get("failed_node_ids")
    assert _exempted_failures(story, "FAILED tests/test_a.py::test_one - x\n") is None


def test_rework_redispatch_with_marker_records_the_baseline(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The other redispatch shape the b3+b4 runs lost: a REWORK redispatch
    onto a worktree that already carries the marker. The story has no
    recorded baseline, so the redispatch must restore it from the marker
    instead of recording nothing."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(
        plan_dir, "baselinerework", {"S1": _fresh_story(status="changes_requested")}
    )
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    marker_ids = ["tests/test_a.py::test_one"]
    (worktree_path / MARKER).write_text(
        json.dumps({"returncode": 1, "failed_node_ids": marker_ids})
    )
    _stub_fresh_dispatch(monkeypatch, worktree_path)

    def _boom(path):
        raise AssertionError("the marker must prevent a second baseline run")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)

    result = p.dispatch_story("baselinerework", "S1")

    assert result["ok"] is True
    story = _story(plan_dir, "baselinerework", "S1")
    baseline = story.get("baseline_test_check")
    assert baseline is not None, "the rework redispatch must record the baseline"
    assert baseline["returncode"] == 1
    assert baseline["failed_node_ids"] == marker_ids


def test_redispatch_keeps_an_already_recorded_baseline(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A redispatch must never WIPE a baseline the story already carries - the
    recorded ids are the only evidence of the pre-dispatch failures."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    recorded = {
        "returncode": 1,
        "failed_node_ids": ["tests/test_a.py::test_one"],
    }
    _write_manifest(
        plan_dir,
        "baselinekept",
        {"S1": _fresh_story(baseline_test_check=recorded)},
    )
    worktree_path = worktree_root / "S1"
    _stub_fresh_dispatch(
        monkeypatch,
        worktree_path,
        preexisting_marker=MARKER,
        preexisting_marker_content=json.dumps(
            {"returncode": 1, "failed_node_ids": recorded["failed_node_ids"]}
        ),
    )

    def _boom(path):
        raise AssertionError("the marker must prevent a second baseline run")

    monkeypatch.setattr(dispatch, "_run_baseline_test_snapshot", _boom)

    result = p.dispatch_story("baselinekept", "S1")

    assert result["ok"] is True
    story = _story(plan_dir, "baselinekept", "S1")
    assert story["baseline_test_check"] == recorded
    stdout = "FAILED tests/test_a.py::test_one - assert False\n"
    assert _exempted_failures(story, stdout) == recorded["failed_node_ids"]


def test_marker_json_records_returncode_and_failed_node_ids(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The marker carries the baseline's returncode AND its failing node ids,
    so a later redispatch can reconstruct the baseline from it alone."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselinepayload", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    _stub_fresh_dispatch(monkeypatch, worktree_path)

    node_ids = ["tests/test_a.py::test_one"]
    monkeypatch.setattr(
        dispatch,
        "_run_baseline_test_snapshot",
        lambda path: {
            "cmd": ["pytest"],
            "returncode": 1,
            "failed_node_ids": node_ids,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )

    result = p.dispatch_story("baselinepayload", "S1")

    assert result["ok"] is True
    payload = _marker_payload(worktree_path)
    assert payload["returncode"] == 1
    assert payload["failed_node_ids"] == node_ids


def test_marker_json_records_a_green_baseline(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A passing baseline is written to the marker too - with rc 0 and no
    failing node ids - so the marker never claims a failure that did not
    happen."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "baselinegreen", {"S1": _fresh_story()})
    worktree_path = worktree_root / "S1"
    _stub_fresh_dispatch(monkeypatch, worktree_path)

    monkeypatch.setattr(
        dispatch,
        "_run_baseline_test_snapshot",
        lambda path: {
            "cmd": ["pytest"],
            "returncode": 0,
            "failed_node_ids": [],
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )

    result = p.dispatch_story("baselinegreen", "S1")

    assert result["ok"] is True
    payload = _marker_payload(worktree_path)
    assert payload["returncode"] == 0
    assert payload["failed_node_ids"] == []


def test_a_green_baseline_is_never_exempted():
    """Negative: a story whose recorded baseline was GREEN must never have its
    failures waived."""
    story = _fresh_story()
    story["baseline_test_check"] = {
        "returncode": 0,
        "failed_node_ids": ["tests/test_a.py::test_one"],
    }

    assert _exempted_failures(story, "FAILED tests/test_a.py::test_one - x\n") is None


def test_baseline_test_check_is_not_a_patchable_story_field():
    """``baseline_test_check`` is pipeline-owned state: it must stay out of
    ``patch_story``'s editable-field set, while the fields that WERE editable
    stay editable."""
    from pipeline import service as pservice

    patchable = pservice._PATCHABLE_STORY_FIELDS

    assert "baseline_test_check" not in patchable
    assert "agent_instructions" in patchable
    assert "summary" in patchable


def test_dispatch_module_stays_under_the_line_limit():
    """The baseline code moved OUT into ``pipeline/story_fields.py``, so
    ``pipeline/dispatch.py`` must not grow past the repo's 1000-line limit."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    lines = sum(1 for _ in (repo_root / "pipeline" / "dispatch.py").open())
    assert_within_line_limit(lines, 1000, "pipeline/dispatch.py")


def test_story_fields_module_stays_under_the_line_limit():
    """The extracted module is production code too, so it is under the limit
    as well."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    lines = sum(1 for _ in (repo_root / "pipeline" / "story_fields.py").open())
    assert_within_line_limit(lines, 1000, "pipeline/story_fields.py")
