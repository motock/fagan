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
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
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


def _stub_fresh_dispatch(monkeypatch, worktree_path, *, preexisting_marker=None):
    """Stub every external boundary a FRESH (non-resuming) local dispatch
    touches so it runs to completion without real git/gh/Plane/subprocess, and
    simulate ``git worktree add`` actually creating the worktree directory -
    optionally already carrying a marker file, exactly as a marker committed
    to the base branch would. Returns the capturing dispatch backend."""
    def _fake_run(cmd, **kw):
        if (
            isinstance(cmd, (list, tuple))
            and list(cmd)[:3] == ["git", "worktree", "add"]
        ):
            worktree_path.mkdir(parents=True, exist_ok=True)
            if preexisting_marker is not None:
                (worktree_path / preexisting_marker).write_text("ok\n")

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


def _fresh_story(status="todo"):
    return {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": status,
        "dependencies": [],
    }


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
