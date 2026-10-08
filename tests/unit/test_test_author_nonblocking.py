"""Non-blocking test-author phase primitives (pipeline/test_author.py):
``start_test_author_phase`` / ``collect_test_author_phase`` /
``_agent_has_exited``.

The scheduler's reconcile worker thread calls ``dispatch_story`` with a
bounded join deadline, so the BLOCKING ``_run_test_author_phase`` (which
polls ``os.waitpid`` in a loop for up to PIPELINE_TEST_AUTHOR_TIMEOUT_
SECONDS, default 5400) can stall every plan behind one slow test-author.
This story adds the NON-BLOCKING primitives a later story will wire in:

  - ``start_test_author_phase`` performs everything the blocking phase does
    BEFORE the wait (doc/config-only skip, ``[no-new-tests]`` opt-out
    skip, backend resolution, ``backend.dispatch`` launch) and returns a
    phase dict ``{"pid", "started_at", "backend", "model"}`` immediately,
    or None on any fall-open. Never raises.
  - ``collect_test_author_phase`` reaps a launched phase without blocking:
    None while the agent is alive and under the timeout; False (after
    SIGTERM + notify) once the timeout elapses; otherwise the commit-
    detection tail (True iff a non-WIP commit landed). Never raises.
  - ``_agent_has_exited`` is the non-blocking liveness probe behind
    collect: True for a reaped/exited pid, False for a live one, False
    for a pid we cannot signal. Never raises.

The agent process is a REAL short-lived subprocess (the only true external
boundary mocked is ``backend.dispatch``, which returns a handle carrying
that pid); commit detection runs against a REAL tmp git worktree.

Run with the project venv:
    .venv/bin/python -m pytest -q tests/unit/test_test_author_nonblocking.py
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import backend
from pipeline import server as p
from pipeline import test_author as ptest_author
from tests.unit._pipeline_mcp_server_test_helpers import (
    _FakeTestAuthorBackend,
    _make_worktree_repo,
)

_STORY_KEY = "S1"
_BRANCH = "agent/s1"


@pytest.fixture(autouse=True)
def _quiet_notify(monkeypatch):
    """Silence operator notifications; individual tests install their own
    recording stub where the notification content is under test."""
    monkeypatch.setattr(ptest_author, "_notify_user", lambda *a, **k: None)


def _sleeping_agent():
    """A real short-lived child process standing in for the dispatched
    test-author agent: alive for ~30s unless the test kills it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _wire_start(monkeypatch, tmp_path, proc):
    """Resolve test_author to a distinct backend and stub the dispatch
    boundary so ``backend.dispatch`` returns a handle carrying the REAL
    child pid. Leaves a real git worktree ready for commit detection."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, _BRANCH)
    fake = _FakeTestAuthorBackend(pid=proc.pid)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    return wt, fake


def _start(monkeypatch, tmp_path, proc):
    wt, fake = _wire_start(monkeypatch, tmp_path, proc)
    phase = ptest_author.start_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key=_STORY_KEY,
        worktree_path=wt,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    return phase, wt, fake


def _commit(wt, message="Add tests for foo"):
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(
        ["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", message],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    )


# ---------- start_test_author_phase: happy path ----------


def test_start_returns_phase_dict_and_returns_immediately(monkeypatch, tmp_path):
    """A successful launch returns {"pid", "started_at", "backend",
    "model"} with an int pid and a parseable ISO-8601 UTC started_at, and
    returns IMMEDIATELY (< 2s) while the child is still alive - the whole
    point of the non-blocking split."""
    proc = _sleeping_agent()
    try:
        started = time.monotonic()
        phase, wt, fake = _start(monkeypatch, tmp_path, proc)
        elapsed = time.monotonic() - started

        assert isinstance(phase, dict)
        assert phase["pid"] == proc.pid
        assert isinstance(phase["pid"], int)
        started_at = datetime.fromisoformat(phase["started_at"])
        assert started_at.tzinfo is not None, "started_at must be timezone-aware"
        assert started_at.utcoffset() == timezone.utc.utcoffset(None)
        assert phase["backend"] == "mlx"
        assert isinstance(phase["model"], str) and phase["model"]
        assert elapsed < 2.0, "start must not block on the agent"
        assert proc.poll() is None, "the child must still be alive"
        assert len(fake.calls) == 1, "exactly one dispatch must be launched"
        assert fake.calls[0]["cwd"] == wt
    finally:
        proc.kill()
        proc.wait()


# ---------- start_test_author_phase: fall-open paths ----------


def test_start_returns_none_for_opted_out_story(monkeypatch, tmp_path):
    """A story carrying the ``[no-new-tests]`` sentinel skips the phase:
    start returns None and never resolves or fetches a backend."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")

    def _boom(*a, **k):
        raise AssertionError("backend.get_backend must not be called")

    monkeypatch.setattr(backend, "get_backend", _boom)
    phase = ptest_author.start_test_author_phase(
        {"agent_instructions": "Move it. [no-new-tests]"},
        story_key=_STORY_KEY,
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert phase is None


def test_start_returns_none_for_unresolvable_backend(monkeypatch, tmp_path):
    """An unconfigured test_author role (no env override, empty registry)
    falls open: start returns None without dispatching."""
    from app import role_registry

    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    def _boom(*a, **k):
        raise AssertionError("backend.get_backend must not be called")

    monkeypatch.setattr(backend, "get_backend", _boom)
    phase = ptest_author.start_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key=_STORY_KEY,
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert phase is None


def test_start_returns_none_when_dispatch_raises(monkeypatch, tmp_path):
    """A dispatch that raises must not raise past start - fall open to
    None (the fail-open contract)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=0, raises=RuntimeError("unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    phase = ptest_author.start_test_author_phase(
        {"agent_instructions": "Build it."},
        story_key=_STORY_KEY,
        worktree_path=tmp_path,
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert phase is None


# ---------- collect_test_author_phase: still running ----------


def test_collect_returns_none_while_alive_under_timeout(monkeypatch, tmp_path):
    """While the agent is alive and the timeout has not elapsed, collect
    returns None (phase still running) and does NOT kill the child."""
    proc = _sleeping_agent()
    try:
        phase, wt, _fake = _start(monkeypatch, tmp_path, proc)
        monkeypatch.setenv("PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS", "5400")

        result = ptest_author.collect_test_author_phase(
            phase,
            story={"agent_instructions": "Build it."},
            story_key=_STORY_KEY,
            worktree_path=wt,
            plan_name="plan",
        )
        assert result is None
        assert proc.poll() is None, "collect must not kill a live, on-time agent"
    finally:
        proc.kill()
        proc.wait()


# ---------- collect_test_author_phase: timeout ----------


def test_collect_sigterms_and_returns_false_after_timeout(monkeypatch, tmp_path):
    """Once elapsed >= PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS and the agent
    is still running, collect SIGTERMs it, notifies the operator, and
    returns False - the same fail-open as the blocking timeout path."""
    proc = _sleeping_agent()
    try:
        phase, wt, _fake = _start(monkeypatch, tmp_path, proc)
        monkeypatch.setenv("PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS", "1")
        notifications = []
        monkeypatch.setattr(
            ptest_author,
            "_notify_user",
            lambda plan, msg, **kwargs: notifications.append((plan, msg)),
        )
        started_at = datetime.fromisoformat(phase["started_at"])
        later = started_at + timedelta(seconds=10)

        result = ptest_author.collect_test_author_phase(
            phase,
            story={"agent_instructions": "Build it."},
            story_key=_STORY_KEY,
            worktree_path=wt,
            plan_name="plan",
            now=later,
        )
        assert result is False
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGTERM, (
            "collect must SIGTERM the over-time agent"
        )
        assert notifications, "the timeout fail-open must be operator-visible"
        assert "timed out" in notifications[0][1]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------- collect_test_author_phase: exited ----------


def test_collect_returns_true_after_exit_with_non_wip_commit(monkeypatch, tmp_path):
    """The agent exited having made a real (non-WIP) commit on the story
    branch: collect returns True."""
    proc = _sleeping_agent()
    try:
        phase, wt, _fake = _start(monkeypatch, tmp_path, proc)
        _commit(wt)
        proc.kill()
        proc.wait()

        result = ptest_author.collect_test_author_phase(
            phase,
            story={"agent_instructions": "Build it."},
            story_key=_STORY_KEY,
            worktree_path=wt,
            plan_name="plan",
        )
        assert result is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_collect_returns_false_after_exit_with_no_commit(monkeypatch, tmp_path):
    """The agent exited cleanly but committed nothing: collect returns
    False (never raises) - fail open to monolithic dispatch."""
    proc = _sleeping_agent()
    try:
        phase, wt, _fake = _start(monkeypatch, tmp_path, proc)
        proc.kill()
        proc.wait()

        result = ptest_author.collect_test_author_phase(
            phase,
            story={"agent_instructions": "Build it."},
            story_key=_STORY_KEY,
            worktree_path=wt,
            plan_name="plan",
        )
        assert result is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------- _agent_has_exited ----------


def test_agent_has_exited_true_for_reaped_pid():
    """A pid that exited and was already reaped counts as exited."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert ptest_author._agent_has_exited(proc.pid) is True


def test_agent_has_exited_false_for_live_pid():
    """A live child process has not exited."""
    proc = _sleeping_agent()
    try:
        assert ptest_author._agent_has_exited(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()


def test_agent_has_exited_false_when_kill_raises_permission_error(
    monkeypatch,
):
    """A pid we cannot signal (os.kill raises PermissionError) exists but
    is not ours to reap: report False (not exited), never raise."""
    proc = _sleeping_agent()
    try:
        real_kill = os.kill

        def _denied(pid, sig):
            if pid == proc.pid and sig == 0:
                raise PermissionError(1, "Operation not permitted")
            return real_kill(pid, sig)

        monkeypatch.setattr(os, "kill", _denied)
        assert ptest_author._agent_has_exited(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()


def test_agent_has_exited_true_for_unreapable_dead_pid(monkeypatch):
    """A pid that is dead but was reaped by someone else (waitpid raises
    ChildProcessError, kill(0) raises ProcessLookupError) counts as
    exited."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    # proc.wait() already reaped it; waitpid now raises ChildProcessError
    # and kill(pid, 0) raises ProcessLookupError -> exited.
    assert ptest_author._agent_has_exited(proc.pid) is True


# ---------- blocking wrapper still composes the primitives ----------


def test_run_test_author_phase_still_blocks_and_returns_bool(monkeypatch, tmp_path):
    """The blocking wrapper keeps its exact contract: it blocks until the
    (already-exited) agent is reaped, then runs commit detection and
    returns a bool. Guards the refactor that re-expresses it as
    start -> wait -> collect."""
    proc = _sleeping_agent()
    try:
        wt, fake = _wire_start(monkeypatch, tmp_path, proc)
        _commit(wt)
        proc.kill()
        proc.wait()

        result = ptest_author._run_test_author_phase(
            {"agent_instructions": "Build it."},
            story_key=_STORY_KEY,
            worktree_path=wt,
            dispatch_backend="ollama",
            local_model="gpt-oss:20b",
            plan_name="plan",
            plan_role_config={"test_author": {"model": "qwen"}},
        )
        assert result is True
        assert len(fake.calls) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
