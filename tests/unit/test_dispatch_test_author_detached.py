"""TA-DETACH-DISPATCH-WIRE: the detached test-author step wired into
``dispatch_story`` (pipeline/dispatch.py).

These tests drive the REAL ``dispatch_story`` entrypoint end to end and
assert on its return value plus the manifest on disk - never on the
primitives directly, so they fail if the wiring is missing. The only faked
boundaries are genuinely external:

* ``backend.get_backend`` -> a fake backend whose ``dispatch`` spawns a REAL
  sleeping subprocess and returns a handle carrying its pid (the test-author
  agent launch), and whose ``complete`` serves the planner;
* ``p._run_planner`` (the planner LLM call) -> a recorder;
* ``pipeline.test_author._worktree_has_non_wip_commits`` (git commit
  detection) -> a controllable verdict;
* ``pipeline.dispatch._create_fresh_worktree`` (the ``git worktree add``
  boundary) -> creates the worktree directory, exactly as the real one does;
* ``subprocess.run`` (git/gh) -> a no-op, delegating ``ps`` to the real
  binary so the phase's liveness probe still works.

Assertions are membership/relationship only: no whole-file hashes, no exact
manifest key sets, no exact totals.

Run with the project venv:
    .venv/bin/python -m pytest -q tests/unit/test_dispatch_test_author_detached.py
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from app import backend
from pipeline import dispatch as pdispatch
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import test_author as ptest_author
from pipeline import ticketing as pt

MARKER = ".tdd_split_test_author_done"
FLAG = "PIPELINE_TEST_AUTHOR_DETACHED"

# Captured before any monkeypatch so the fake ``subprocess.run`` can delegate
# the liveness probe's ``ps`` call to the real binary.
_REAL_RUN = subprocess.run

_BASE_STORY = {
    "summary": "Do thing",
    "agent_instructions": "Build it.",
    "status": "todo",
    "dependencies": [],
}

# The registry ships no ``test_author`` role, so the plan's role_config is
# what makes the REAL start_test_author_phase resolve a provider/model that
# differs from dispatch (an unconfigured role deliberately skips the split).
_ROLE_CONFIG = {"test_author": {"provider": "mlx", "model": "qwen"}}


# ---------- Fixtures (copied per this repo's convention) ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
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


# ---------- Helpers ----------
def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": stories, "role_config": _ROLE_CONFIG},
            indent=2,
        )
    )


def _story(plan_dir, plan_name):
    raw = (plan_dir / f"{plan_name}.manifest.json").read_text()
    return json.loads(raw)["stories"]["S1"]


class _FakeBackend:
    """Stands in for ``backend.get_backend(...)``: ``dispatch`` launches a
    REAL sleeping subprocess (so the phase's pid is a live child) and
    ``complete`` serves the planner."""

    def __init__(self):
        self.dispatch_calls = []
        self.procs = []

    def dispatch(self, **kwargs):
        proc = subprocess.Popen(["sleep", "30"])
        self.procs.append(proc)
        self.dispatch_calls.append(kwargs)
        return backend.AgentHandle(pid=proc.pid)

    def complete(self, prompt, **kwargs):
        return "1. Write a failing test.\n2. Implement it."

    def kill_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
                    proc.wait(timeout=10)


def _is_test_author_call(kwargs):
    """The test-author launch is the one carrying the phase's system prompt;
    the executor launch carries the persona prompt instead."""
    return kwargs.get("system") == ptest_author._TEST_AUTHOR_SYSTEM


def _stub_externals(monkeypatch, fake_backend, planner_calls):
    """Stub the external boundaries dispatch_story touches."""

    def _fake_run(cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ps":
            return _REAL_RUN(cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_create_worktree(plan_name, branch, worktree_path):
        worktree_path.mkdir(parents=True, exist_ok=True)

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append({"agent_instructions": agent_instructions, **kwargs})
        return "1. Write a failing test.\n2. Implement it."

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake_backend)
    monkeypatch.setattr(pdispatch, "_create_fresh_worktree", _fake_create_worktree)
    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )


def _enable_flag(monkeypatch, value="1"):
    monkeypatch.setenv(FLAG, value)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")


def _wait_for_exit(proc):
    proc.terminate()
    proc.wait(timeout=10)


# ---------- flag ON: first call parks on the detached phase ----------
def test_flag_on_first_call_returns_pending_without_launching_executor(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The headline wiring: with the flag on, the first dispatch launches the
    detached test-author phase, persists its pid, and returns immediately
    with a ``pending`` verdict - the executor is NOT launched and the planner
    is NOT called."""
    _enable_flag(monkeypatch)
    _write_manifest(plan_dir, "detach1", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    started = time.monotonic()
    result = p.dispatch_story("detach1", "S1")
    elapsed = time.monotonic() - started

    assert result["ok"] is True
    assert result.get("pending") == "test_author"
    assert result.get("story_key") == "S1"
    # Non-blocking: the call must not wait on the child.
    assert elapsed < 2.0

    story = _story(plan_dir, "detach1")
    phase = story.get("test_author_phase")
    assert isinstance(phase, dict)
    assert phase["pid"] == fake.procs[0].pid
    assert phase["pid"] > 0

    # Exactly one launch, and it is the test-author phase - no executor.
    assert len(fake.dispatch_calls) == 1
    assert _is_test_author_call(fake.dispatch_calls[0])
    assert planner_calls == []

    fake.kill_all()


# ---------- flag ON: re-entry while the child is alive stays pending ----------
def test_flag_on_second_call_while_child_alive_stays_pending_no_second_child(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Re-entry while the phase is still running must poll, not relaunch: the
    same pending verdict, still no executor, and no second child."""
    _enable_flag(monkeypatch)
    _write_manifest(plan_dir, "detach2", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    assert p.dispatch_story("detach2", "S1").get("pending") == "test_author"
    first_pid = fake.procs[0].pid

    second = p.dispatch_story("detach2", "S1")

    assert second["ok"] is True
    assert second.get("pending") == "test_author"
    assert len(fake.dispatch_calls) == 1
    assert len(fake.procs) == 1
    assert _story(plan_dir, "detach2")["test_author_phase"]["pid"] == first_pid
    assert planner_calls == []

    fake.kill_all()


# ---------- flag ON: child exits having committed -> executor runs, not resumed ----------
def test_flag_on_child_commits_then_executor_launches_not_resumed(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The subtle part: the first call created the worktree, so the re-entry
    call sees ``resuming`` True even though the executor never ran. Once the
    child exits having committed, the executor must launch with
    ``resumed`` False, the planner must run, ``tdd_split`` must be set, and
    the phase field must be dropped."""
    _enable_flag(monkeypatch)
    _write_manifest(plan_dir, "detach3", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)
    monkeypatch.setattr(
        ptest_author, "_worktree_has_non_wip_commits", lambda *a, **k: True
    )

    assert p.dispatch_story("detach3", "S1").get("pending") == "test_author"
    _wait_for_exit(fake.procs[0])

    result = p.dispatch_story("detach3", "S1")

    assert result["ok"] is True
    assert result.get("resumed") is False
    assert result.get("pending") is None

    story = _story(plan_dir, "detach3")
    assert story.get("tdd_split") is True
    assert "test_author_phase" not in story
    assert (worktree_root / "S1" / MARKER).exists()

    # The planner ran (the executor is a fresh, non-resumed launch) and the
    # executor itself was launched as the second dispatch call.
    assert len(planner_calls) == 1
    assert len(fake.dispatch_calls) == 2
    assert _is_test_author_call(fake.dispatch_calls[0])
    assert not _is_test_author_call(fake.dispatch_calls[1])

    fake.kill_all()


# ---------- flag ON: child exits with NO commit -> fail open, executor still runs ----------
def test_flag_on_child_exits_without_commit_fails_open_to_executor(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A phase that produced no finished commit must fail open: the executor
    still launches, not resumed, with no marker and no phase field."""
    _enable_flag(monkeypatch)
    _write_manifest(plan_dir, "detach4", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)
    monkeypatch.setattr(
        ptest_author, "_worktree_has_non_wip_commits", lambda *a, **k: False
    )

    assert p.dispatch_story("detach4", "S1").get("pending") == "test_author"
    _wait_for_exit(fake.procs[0])

    result = p.dispatch_story("detach4", "S1")

    assert result["ok"] is True
    assert result.get("resumed") is False

    story = _story(plan_dir, "detach4")
    assert "test_author_phase" not in story
    assert story.get("tdd_split") is not True
    assert not (worktree_root / "S1" / MARKER).exists()

    assert len(fake.dispatch_calls) == 2
    assert not _is_test_author_call(fake.dispatch_calls[1])

    fake.kill_all()


# ---------- flag OFF / '0' / 'garbage': today's blocking path, unchanged ----------
@pytest.mark.parametrize("value", ["0", "garbage", ""])
def test_flag_off_runs_blocking_helper_and_never_writes_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch, value,
):
    """With the flag off (or any non-truthy value) the new step declines and
    today's blocking helper runs exactly once; the detached phase field is
    never written."""
    monkeypatch.setenv(FLAG, value)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "detachoff", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    blocking_calls = []

    def _fake_blocking(*args, **kwargs):
        blocking_calls.append((args, kwargs))
        return False

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_blocking)

    result = p.dispatch_story("detachoff", "S1")

    assert result["ok"] is True
    assert len(blocking_calls) == 1
    story = _story(plan_dir, "detachoff")
    assert "test_author_phase" not in story
    assert not (worktree_root / "S1" / MARKER).exists()

    fake.kill_all()


def test_flag_unset_runs_blocking_helper_and_never_writes_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The flag is opt-in: unset means the detached step declines."""
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "detachunset", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    blocking_calls = []
    monkeypatch.setattr(
        p,
        "_run_test_author_phase",
        lambda *a, **k: blocking_calls.append((a, k)) or False,
    )

    result = p.dispatch_story("detachunset", "S1")

    assert result["ok"] is True
    assert len(blocking_calls) == 1
    assert "test_author_phase" not in _story(plan_dir, "detachunset")

    fake.kill_all()


# ---------- flag ON but non-local backend: declines, blocking path runs ----------
def test_flag_on_non_local_backend_declines_to_blocking_path(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The detached step is local-family only; a claude dispatch must fall
    through to today's path with no phase field. (Today's blocking helper is
    itself local-family-only, so neither path runs here - the point is that
    the new step does not hijack a claude dispatch.)"""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    _write_manifest(plan_dir, "detachclaude", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    blocking_calls = []
    monkeypatch.setattr(
        p,
        "_run_test_author_phase",
        lambda *a, **k: blocking_calls.append((a, k)) or False,
    )

    result = p.dispatch_story("detachclaude", "S1")

    assert result["ok"] is True
    assert blocking_calls == []
    assert "test_author_phase" not in _story(plan_dir, "detachclaude")

    fake.kill_all()


def test_worktree_path_is_created_by_the_first_call(plan_dir, worktree_root, agents_dir, monkeypatch):
    """Guard for the subtle part: the first call must leave the worktree on
    disk (that is what makes the re-entry's ``resuming`` True), and the
    re-entry must still not be treated as an executor resume."""
    _enable_flag(monkeypatch)
    _write_manifest(plan_dir, "detachwt", {"S1": dict(_BASE_STORY)})
    fake = _FakeBackend()
    planner_calls = []
    _stub_externals(monkeypatch, fake, planner_calls)

    worktree = worktree_root / "S1"
    assert not worktree.exists()

    p.dispatch_story("detachwt", "S1")

    assert worktree.exists()
    assert Path(worktree).is_dir()

    fake.kill_all()
