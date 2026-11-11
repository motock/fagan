"""Tests for pipeline/dispatch_fence.py — the abandoned-dispatch fence.

Defect (b3+b4 runs, standalone stack, 2026-11-09/10): the scheduler's
reconcile worker is abandoned when it passes its 900s join deadline
(``scheduler_daemon._run_with_watchdog``). A thread cannot be killed, so the
abandoned worker kept running its dispatch and, after the daemon restarted,
spawned an agent on a worktree the fresh daemon was dispatching too: two
agents, one worktree.

The fence has three layers, all graded here through the public API:

- ``dispatch_side_effect_in_progress(worktree)`` — is the worktree showing
  work in progress (the baseline marker, or commits beyond the base branch)?
- ``blocks_reclaim(story, worktree)`` — should a reclaim be withheld because
  a previous dispatch's scheduler is gone but its worktree still shows work?
- ``mark_worker_abandoned(thread)`` / ``worker_is_abandoned(thread)`` — the
  in-process registry that turns "the worker cannot be killed" into "the
  worker cannot launch an agent".

Every helper must be fail-safe: an unreadable / absent worktree must never
raise and must never withhold work.

The module is imported lazily through :func:`_fence` so a missing
implementation FAILS on an assertion (not a collection-time ImportError).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import advance as advance_module
from pipeline import concurrency
from pipeline import config as config_module
from pipeline import dispatch as dispatch_module
from pipeline import server as p
from pipeline.store_records import validate_plan_name

_PLAN = "dispatch-fence-plan"
_MARKER = ".dispatch_baseline_test_checked"


def _fence():
    """Import pipeline.dispatch_fence, failing on an assertion when absent."""
    try:
        return importlib.import_module("pipeline.dispatch_fence")
    except ImportError as exc:  # pragma: no cover - pre-implementation
        pytest.fail(
            "pipeline/dispatch_fence.py must exist and export "
            "dispatch_side_effect_in_progress / blocks_reclaim / "
            f"mark_worker_abandoned / worker_is_abandoned ({exc})"
        )


def _iso_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _dead_pid() -> int:
    """A pid that is guaranteed not to be running (never our own)."""
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    pytest.fail("no free pid found for the dead-owner fixture")


def _live_foreign_pid() -> int:
    """A pid that is running right now and is not this process."""
    if os.getpid() != 1:
        try:
            os.kill(1, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return 1
        else:
            return 1
    pytest.fail("pid 1 is this process: no live foreign pid available")



# ---------------------------------------------------------------------------
# 1. dispatch_side_effect_in_progress
# ---------------------------------------------------------------------------


def test_side_effect_is_detected_by_the_baseline_marker(tmp_path):
    """A worktree carrying .dispatch_baseline_test_checked means the previous
    dispatch got as far as the baseline-test phase - work in progress."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    assert fence.dispatch_side_effect_in_progress(worktree) is True


def test_side_effect_is_detected_by_commits_beyond_the_base(tmp_path):
    """A branch with commits beyond its base is work in progress too, even
    without the baseline marker."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert fence.dispatch_side_effect_in_progress(worktree) is False

    # The implementation probes git for "commits beyond the base"; give it a
    # real repository with a real base so the probe has something to read.
    remote = tmp_path / "remote.git"

    def _run(*args, cwd=None):
        subprocess.run(
            list(args), cwd=str(cwd or worktree), check=True, capture_output=True
        )

    _run("git", "init", "-b", "main")
    _run("git", "config", "user.email", "fence@example.com")
    _run("git", "config", "user.name", "Fence")
    (worktree / "file.txt").write_text("one\n")
    _run("git", "add", "file.txt")
    _run("git", "commit", "-m", "base")

    # A story branch, plus an origin whose HEAD is the base branch, so the
    # "commits beyond the base" probe resolves the same way a real worktree
    # of a real repo does.
    _run("git", "clone", "--bare", str(worktree), str(remote))
    _run("git", "remote", "add", "origin", str(remote))
    _run("git", "fetch", "origin")
    _run("git", "remote", "set-head", "origin", "-a")
    _run("git", "checkout", "-b", "feature/story")

    assert fence.dispatch_side_effect_in_progress(worktree) is False, (
        "a branch with no commits beyond its base is NOT work in progress"
    )

    (worktree / "file.txt").write_text("two\n")
    _run("git", "add", "file.txt")
    _run("git", "commit", "-m", "work")

    assert fence.dispatch_side_effect_in_progress(worktree) is True, (
        "a branch with commits beyond its base IS work in progress"
    )


def test_absent_worktree_is_not_a_side_effect_and_never_raises(tmp_path):
    """A worktree path that does not exist is not a side effect, and the
    helper must not raise."""
    fence = _fence()

    assert fence.dispatch_side_effect_in_progress(tmp_path / "nope") is False


def test_unreadable_worktree_is_not_a_side_effect_and_never_raises(tmp_path):
    """An unreadable worktree fails SAFE (returns False, withholds nothing)
    and never raises."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")
    # Make the directory unreadable so even the marker probe fails.
    worktree.chmod(0o000)
    try:
        assert fence.dispatch_side_effect_in_progress(worktree) is False
    finally:
        worktree.chmod(0o755)


def test_a_worktree_path_given_as_a_string_is_accepted(tmp_path):
    """The helper takes a path; a plain string must work like a Path."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    assert fence.dispatch_side_effect_in_progress(str(worktree)) is True


# ---------------------------------------------------------------------------
# 2. blocks_reclaim
# ---------------------------------------------------------------------------


def test_dead_owner_with_side_effect_blocks_the_reclaim(tmp_path):
    """The core defect: the lease owner is gone, but the worktree still shows
    work in progress - the reclaim must be blocked."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    story = {
        "dispatch_lease_expires_at": _iso_in(600),
        "dispatch_lease_owner_pid": _dead_pid(),
    }

    assert fence.blocks_reclaim(story, worktree) is True


def test_expired_lease_without_a_side_effect_does_not_block(tmp_path):
    """An expired lease with no side effect must NOT withhold work: the story
    is re-claimable."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()

    story = {
        "dispatch_lease_expires_at": _iso_in(-60),
        "dispatch_lease_owner_pid": _dead_pid(),
    }

    assert fence.blocks_reclaim(story, worktree) is False


def test_a_live_lease_does_not_block(tmp_path):
    """A lease this process still owns is live - the reclaim path never sees
    it as reclaimable, so it must not report 'blocked'."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    story = {
        "dispatch_lease_expires_at": _iso_in(600),
        "dispatch_lease_owner_pid": os.getpid(),
    }

    assert fence.blocks_reclaim(story, worktree) is False


def test_no_lease_at_all_does_not_block(tmp_path):
    """No lease means nothing to reclaim over - work must not be withheld."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    assert fence.blocks_reclaim({}, worktree) is False


def test_a_malformed_lease_never_raises(tmp_path):
    """A corrupt lease field must never make the helper raise - the fence is
    consulted on every dispatch-eligible story."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")

    for story in (
        {"dispatch_lease_expires_at": "not-a-timestamp"},
        {"dispatch_lease_expires_at": "2026-01-01T00:00:00"},  # naive
        {"dispatch_lease_expires_at": None},
        {"dispatch_lease_expires_at": ""},
        {"dispatch_lease_owner_pid": "not-a-pid"},
    ):
        result = fence.blocks_reclaim(story, worktree)
        assert isinstance(result, bool), story


def test_blocks_reclaim_does_not_raise_for_an_unreadable_worktree(tmp_path):
    """An unreadable worktree fails safe: no exception, and no block."""
    fence = _fence()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / _MARKER).write_text("ok\n")
    worktree.chmod(0o000)
    story = {
        "dispatch_lease_expires_at": _iso_in(600),
        "dispatch_lease_owner_pid": _dead_pid(),
    }
    try:
        assert fence.blocks_reclaim(story, worktree) is False
    finally:
        worktree.chmod(0o755)


def test_blocks_reclaim_does_not_raise_for_an_absent_worktree(tmp_path):
    """An absent worktree fails safe: no exception, and no block."""
    fence = _fence()
    story = {
        "dispatch_lease_expires_at": _iso_in(600),
        "dispatch_lease_owner_pid": _dead_pid(),
    }

    assert fence.blocks_reclaim(story, tmp_path / "nope") is False


# ---------------------------------------------------------------------------
# 3. The in-process abandonment registry
# ---------------------------------------------------------------------------


def test_an_abandoned_worker_is_recognised():
    """mark_worker_abandoned / worker_is_abandoned round-trip over a live
    thread (the watchdog abandons a worker that is still running)."""
    fence = _fence()
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(30), daemon=True)
    thread.start()
    try:
        fence.mark_worker_abandoned(thread)
        assert fence.worker_is_abandoned(thread) is True
    finally:
        release.set()
        thread.join(timeout=30)


def test_an_unmarked_thread_is_not_abandoned():
    """A thread that was never marked is not reported as abandoned. The
    CURRENT thread is used: its identity cannot be recycled while it is
    alive, so the assertion cannot be perturbed by a dead worker's entry."""
    fence = _fence()

    assert fence.worker_is_abandoned(threading.current_thread()) is False


def test_the_registry_forgets_a_dead_thread():
    """An entry is removed when the thread dies, so a long-lived process does
    not accumulate stale identities."""
    fence = _fence()
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(30), daemon=True)
    thread.start()
    fence.mark_worker_abandoned(thread)
    assert fence.worker_is_abandoned(thread) is True

    release.set()
    thread.join(timeout=30)
    assert not thread.is_alive()

    for _ in range(100):
        if fence.worker_is_abandoned(thread) is False:
            break
        threading.Event().wait(0.02)
    assert fence.worker_is_abandoned(thread) is False, (
        "a dead thread must be dropped from the abandonment registry"
    )


# ---------------------------------------------------------------------------
# 4. advance: a dead-owner lease with a side effect is NOT re-claimed
# ---------------------------------------------------------------------------


class _RecordingStore:
    """A minimal record store whose get_manifest reflects its put_manifest."""

    def __init__(self, path, manifest):
        self.path = path
        self.manifest = manifest
        self.events = []
        self.writes = []
        self.path.write_text(json.dumps(manifest))

    def manifest_path(self, plan_name):
        return self.path

    def records(self):
        return self

    def get_manifests(self, namespace):
        return {}

    def get_manifest(self, *args):
        if len(args) == 1:
            return json.loads(self.path.read_text())
        _namespace, plan_name = args
        validate_plan_name(plan_name)
        self.events.append(("get_manifest", plan_name))
        if self.manifest is None:
            return None
        return json.loads(json.dumps(self.manifest))

    def put_manifest(self, namespace, plan_name, manifest):
        validate_plan_name(plan_name)
        self.events.append(("put_manifest", plan_name))
        self.writes.append(plan_name)
        self.manifest = json.loads(json.dumps(manifest))
        self.path.write_text(json.dumps(manifest))


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


def _seed(monkeypatch, store, notifications):
    """Wire every seam the tick body touches, wide open."""
    monkeypatch.setattr(advance_module, "_store", store)
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "_count_on_device_in_progress_agents", lambda: 0)
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(
        advance_module, "_notify_user", lambda *a, **k: notifications.append((a, k))
    )
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 5, raising=False)
    monkeypatch.setattr(
        advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 5, raising=False
    )
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    for name in (
        "SESSION_PAUSE_THRESHOLD",
        "SESSION_RESUME_THRESHOLD",
        "WEEK_PAUSE_THRESHOLD",
        "WEEK_RESUME_THRESHOLD",
    ):
        monkeypatch.setattr(config_module, name, 101, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, 101, raising=False)


def _manifest(stories):
    return {"name": _PLAN, "paused": False, "stories": stories}


def _story(status="todo", **extra):
    story = {
        "key": "s1",
        "title": "story",
        "status": status,
        "risk": "low",
        "dispatch_attempts": 0,
    }
    story.update(extra)
    return story


def _run_tick(plan_name=_PLAN):
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


def _worktree_with_marker(tmp_path):
    """A worktree that shows work in progress (the baseline marker)."""
    worktree = tmp_path / "worktrees" / "s1"
    worktree.mkdir(parents=True)
    (worktree / _MARKER).write_text("ok\n")
    return worktree


def test_advance_skips_a_story_whose_previous_dispatch_is_still_in_progress(
    monkeypatch, tmp_path
):
    """The dead-owner lease plus a worktree that shows work in progress must
    NOT be re-claimed: the story is skipped, no dispatch happens, and the
    attempt counter is NOT bumped."""
    _fence()  # the module must exist for the tick to consult it
    dead = _dead_pid()
    worktree = _worktree_with_marker(tmp_path)
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest(
            {
                "s1": _story(
                    worktree=str(worktree),
                    dispatch_lease_expires_at=_iso_in(600),
                    dispatch_lease_owner_pid=dead,
                )
            }
        ),
    )
    notifications = []
    _seed(monkeypatch, store, notifications)

    dispatched = []
    monkeypatch.setattr(
        advance_module,
        "dispatch_story",
        lambda plan_name, key: dispatched.append(key) or {"ok": True},
    )

    _run_tick()

    assert dispatched == [], (
        f"the story must not be dispatched while its previous dispatch is "
        f"still in progress: {dispatched}"
    )
    story = store.manifest["stories"]["s1"]
    assert story.get("dispatch_attempts", 0) == 0, (
        f"a blocked reclaim must not consume a dispatch attempt: "
        f"{story.get('dispatch_attempts')}"
    )
    assert story["status"] == "todo"
    # The lease was NOT re-claimed by this process.
    assert story.get("dispatch_lease_owner_pid") == dead, (
        f"the dead owner's lease must be left alone: {story}"
    )


def test_advance_notifies_once_when_the_previous_dispatch_is_in_progress(
    monkeypatch, tmp_path
):
    """The skip is reported with event="dispatch_in_progress", exactly once
    per story."""
    _fence()
    dead = _dead_pid()
    worktree = _worktree_with_marker(tmp_path)
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest(
            {
                "s1": _story(
                    worktree=str(worktree),
                    dispatch_lease_expires_at=_iso_in(600),
                    dispatch_lease_owner_pid=dead,
                )
            }
        ),
    )
    notifications = []
    _seed(monkeypatch, store, notifications)
    monkeypatch.setattr(
        advance_module, "dispatch_story", lambda plan_name, key: {"ok": True}
    )

    _run_tick()

    events = [k.get("event") for _a, k in notifications]
    assert events.count("dispatch_in_progress") == 1, (
        f"exactly one dispatch_in_progress notification per story: {notifications}"
    )
    # ``_notify_user(plan_name, message, ...)`` takes the message positionally.
    message = next(
        (a[1] if len(a) > 1 else k.get("message", ""))
        for a, k in notifications
        if k.get("event") == "dispatch_in_progress"
    )
    assert isinstance(message, str) and message.strip(), (
        "the dispatch_in_progress notification must carry a message"
    )


def test_advance_reclaims_an_expired_lease_without_a_side_effect(
    monkeypatch, tmp_path
):
    """An expired lease with no work in progress must NOT withhold work: the
    story is dispatched."""
    _fence()
    dead = _dead_pid()
    worktree = tmp_path / "worktrees" / "s1"
    worktree.mkdir(parents=True)  # exists, but shows NO work in progress
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest(
            {
                "s1": _story(
                    worktree=str(worktree),
                    dispatch_lease_expires_at=_iso_in(-60),
                    dispatch_lease_owner_pid=dead,
                )
            }
        ),
    )
    notifications = []
    _seed(monkeypatch, store, notifications)

    dispatched = []
    monkeypatch.setattr(
        advance_module,
        "dispatch_story",
        lambda plan_name, key: dispatched.append(key) or {"ok": True},
    )

    _run_tick()

    assert dispatched == ["s1"], (
        f"an expired lease with no side effect must not withhold the work: {dispatched}"
    )
    events = [k.get("event") for _a, k in notifications]
    assert "dispatch_in_progress" not in events


def test_advance_does_not_block_when_the_worktree_is_unreadable(
    monkeypatch, tmp_path
):
    """An unreadable worktree fails safe: the work is still dispatched."""
    _fence()
    dead = _dead_pid()
    worktree = _worktree_with_marker(tmp_path)
    worktree.chmod(0o000)
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest(
            {
                "s1": _story(
                    worktree=str(worktree),
                    dispatch_lease_expires_at=_iso_in(600),
                    dispatch_lease_owner_pid=dead,
                )
            }
        ),
    )
    notifications = []
    _seed(monkeypatch, store, notifications)

    dispatched = []
    monkeypatch.setattr(
        advance_module,
        "dispatch_story",
        lambda plan_name, key: dispatched.append(key) or {"ok": True},
    )

    try:
        _run_tick()
    finally:
        worktree.chmod(0o755)

    assert dispatched == ["s1"], (
        f"an unreadable worktree must not withhold work: {dispatched}"
    )


# ---------------------------------------------------------------------------
# 5. dispatch: the pre-spawn fence
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class _CapturingDispatchBackend:
    """Stands in for backend.get_backend(...): records every spawn attempt."""

    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeProc(4242)

    def complete(self, prompt, **kwargs):
        return ""


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    from pipeline import persistence as ppers

    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _fresh_story(status="todo", **overrides):
    story = {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": status,
        "dependencies": [],
    }
    story.update(overrides)
    return story


def _stub_dispatch(monkeypatch, worktree_path):
    """Stub every external boundary so a dispatch runs to the spawn point."""
    from app import backend
    from pipeline import ticketing as pt

    def _fake_run(cmd, **kw):
        if (
            isinstance(cmd, (list, tuple))
            and list(cmd)[:3] == ["git", "worktree", "add"]
        ):
            worktree_path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        backend.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc(9500)
    )
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_provision_worktree_venv", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)
    capturing = _CapturingDispatchBackend()
    monkeypatch.setattr(backend, "get_backend", lambda role, name=None: capturing)
    return capturing


def _story_from_disk(plan_dir, plan_name, story_key):
    manifest = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())
    return manifest["stories"][story_key]


def test_a_lease_lost_between_claim_and_spawn_launches_no_agent(
    plan_dir, worktree_root, monkeypatch
):
    """The lease is lost (another LIVE process holds it) before the agent is
    spawned: NO agent may be launched and the story status must be untouched.

    A lease whose owner pid is provably DEAD is deliberately NOT evidence of
    a lost claim: dispatch_story is also called with no scheduler lease at
    all, so only a live foreign owner positively proves the claim was lost.
    """
    _fence()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(
        plan_dir,
        "fence-lost",
        {
            "S1": _fresh_story(
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=_live_foreign_pid(),
            )
        },
    )
    capturing = _stub_dispatch(monkeypatch, worktree_root / "S1")

    result = p.dispatch_story("fence-lost", "S1")

    assert capturing.calls == [], (
        f"no agent may be spawned once the lease is lost: {capturing.calls}"
    )
    assert result["ok"] is False
    assert result["error"] == "dispatch lease lost"
    story = _story_from_disk(plan_dir, "fence-lost", "S1")
    assert story["status"] == "todo", (
        f"the story status must be untouched when the fence aborts: {story['status']}"
    )
    assert "pid" not in story


def test_a_story_that_is_already_terminal_launches_no_agent(
    plan_dir, worktree_root, monkeypatch
):
    """The story reached a terminal status (another actor finished it) while
    the dispatch was in its slow phases: NO agent may be launched.

    Only ``done``/``merged`` are positive evidence of an unwanted spawn; any
    other status still proceeds, because ``dispatch_story`` itself drives the
    story from ``todo``/``interrupted``/``changes_requested`` to
    ``in_progress`` and a stray intermediate status must never withhold work.
    """
    _fence()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(
        plan_dir,
        "fence-status",
        {
            "S1": _fresh_story(
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=os.getpid(),
            )
        },
    )
    capturing = _stub_dispatch(monkeypatch, worktree_root / "S1")

    # Another actor advances the story while this dispatch is still in its
    # slow phases (after the manifest was last persisted by the dispatch).
    real_snapshot = dispatch_module._run_baseline_test_snapshot

    def _advance_then_snapshot(path):
        payload = json.loads((plan_dir / "fence-status.manifest.json").read_text())
        payload["stories"]["S1"]["status"] = "done"
        (plan_dir / "fence-status.manifest.json").write_text(json.dumps(payload))
        return real_snapshot(path)

    monkeypatch.setattr(
        dispatch_module, "_run_baseline_test_snapshot", _advance_then_snapshot
    )

    result = p.dispatch_story("fence-status", "S1")

    assert capturing.calls == [], (
        f"no agent may be spawned for a story that is no longer "
        f"dispatch-eligible: {capturing.calls}"
    )
    assert result["ok"] is False
    assert isinstance(result.get("error"), str) and result["error"].strip()
    story = _story_from_disk(plan_dir, "fence-status", "S1")
    assert story.get("pid") is None, (
        f"no agent pid may be recorded when the fence aborts: {story}"
    )


def test_an_abandoned_worker_launches_no_agent(plan_dir, worktree_root, monkeypatch):
    """The abandoned worker cannot launch an agent: the fence aborts with
    error="dispatch abandoned" BEFORE the lease check."""
    fence = _fence()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(
        plan_dir,
        "fence-abandoned",
        {
            "S1": _fresh_story(
                # The lease is still owned by THIS process: only the
                # abandonment registry can stop this spawn.
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=os.getpid(),
            )
        },
    )
    capturing = _stub_dispatch(monkeypatch, worktree_root / "S1")

    # Model the watchdog abandoning this dispatch's worker mid-flight: the
    # thread that runs the dispatch is registered as abandoned before the
    # pre-spawn fence runs. The dispatch runs on its own thread so the
    # registry entry dies with it and cannot leak into other tests.
    real_record_branch = dispatch_module.record_story_branch

    def _abandon_then_record(story, story_key):
        fence.mark_worker_abandoned(threading.current_thread())
        return real_record_branch(story, story_key)

    monkeypatch.setattr(dispatch_module, "record_story_branch", _abandon_then_record)

    box = {}

    def _run():
        try:
            box["result"] = p.dispatch_story("fence-abandoned", "S1")
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=120)
    assert not worker.is_alive(), "the dispatch thread did not finish"
    if "error" in box:
        raise box["error"]
    result = box["result"]

    assert capturing.calls == [], (
        f"an abandoned worker must not launch an agent: {capturing.calls}"
    )
    assert result["ok"] is False
    assert result["error"] == "dispatch abandoned"
    story = _story_from_disk(plan_dir, "fence-abandoned", "S1")
    assert story["status"] == "todo", (
        f"the story status must be untouched when the fence aborts: {story['status']}"
    )


# ---------------------------------------------------------------------------
# 6. scheduler_daemon: the watchdog registers the abandoned worker
# ---------------------------------------------------------------------------


def test_the_watchdog_registers_the_abandoned_worker(monkeypatch):
    """When the reconcile worker passes its join deadline, the watchdog must
    register it as abandoned, so the worker cannot go on to launch an agent.

    The registry is what the item-3 pre-spawn fence consults, so the wiring
    between the two is the whole point of this test.
    """
    fence = _fence()
    from pipeline import scheduler_daemon as sd
    from pipeline.events import InProcessEventBus

    monkeypatch.setattr(sd, "_reconcile_join_timeout_seconds", lambda: 0.05)
    monkeypatch.setattr(sd, "_scan_join_timeout_seconds", lambda: 0.05)

    release = threading.Event()

    def _wedged_reconcile():
        release.wait(30)

    daemon = sd.SchedulerDaemon(
        reconcile_fn=_wedged_reconcile,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
    )

    try:
        completed, _outcome = daemon._run_with_watchdog(
            _wedged_reconcile, timeout_s=0.05, label="reconcile"
        )
    finally:
        release.set()

    assert completed is False, (
        "the wedged worker must be reported as not completed (abandoned)"
    )
    abandoned = [
        worker
        for worker, _ts in getattr(daemon, "_abandoned_workers", [])
        if fence.worker_is_abandoned(worker)
    ]
    assert abandoned, (
        "the watchdog must register the abandoned worker with the dispatch "
        "fence so it cannot launch an agent"
    )


def test_a_lease_owned_by_this_process_still_spawns(
    plan_dir, worktree_root, monkeypatch
):
    """The happy path: this process owns the lease, so the agent is spawned
    normally."""
    _fence()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(
        plan_dir,
        "fence-owned",
        {
            "S1": _fresh_story(
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=os.getpid(),
            )
        },
    )
    capturing = _stub_dispatch(monkeypatch, worktree_root / "S1")

    result = p.dispatch_story("fence-owned", "S1")

    assert result["ok"] is True, result
    assert len(capturing.calls) == 1, (
        f"the agent must be spawned normally when this process owns the lease: "
        f"{capturing.calls}"
    )


# ---------------------------------------------------------------------------
# 6. executor_fence refuses ONLY on positive evidence
#
# ``dispatch_story`` is also called with no scheduler lease at all (the MCP
# ``dispatch_story`` tool, startup reconcile, and direct callers), so absent
# lease fields, an unacquirable plan lock, or an unreadable manifest must
# never withhold work. Only another live owner, an abandoned worker, or a
# terminal status positively prove a spawn is unwanted.
# ---------------------------------------------------------------------------


class _PathStore:
    """Just enough store for ``executor_fence``: the persisted manifest path."""

    def __init__(self, path):
        self._path = path

    def manifest_path(self, plan_name):
        return self._path


def test_executor_fence_proceeds_when_the_story_has_no_lease(
    plan_dir, monkeypatch
):
    """No lease at all (manual dispatch): the fence must not refuse."""
    fence = _fence()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "fence-nolease", {"S1": _fresh_story()})
    store = _PathStore(plan_dir / "fence-nolease.manifest.json")

    assert fence.executor_fence("fence-nolease", "S1", store) is None, (
        "a story with no lease fields must still be dispatchable"
    )


def test_executor_fence_proceeds_when_the_plan_lock_is_unavailable(
    plan_dir, monkeypatch
):
    """Lock contention is not evidence of a lost claim: proceed."""
    fence = _fence()
    _write_manifest(
        plan_dir,
        "fence-locked",
        {"S1": _fresh_story(dispatch_lease_expires_at=_iso_in(600))},
    )
    store = _PathStore(plan_dir / "fence-locked.manifest.json")

    from contextlib import contextmanager

    @contextmanager
    def _never_acquired(plan_name, wait_seconds=0.0):
        yield False

    monkeypatch.setattr(concurrency, "_plan_lock", _never_acquired)

    assert fence.executor_fence("fence-locked", "S1", store) is None, (
        "an unacquirable plan lock must not withhold work"
    )


def test_executor_fence_proceeds_when_the_manifest_is_unreadable(
    plan_dir, monkeypatch, caplog
):
    """An unreadable manifest must not raise and must not refuse."""
    fence = _fence()
    store = _PathStore(plan_dir / "fence-garbled.manifest.json")
    store.manifest_path("fence-garbled").write_text("{not json")

    with caplog.at_level("WARNING"):
        assert fence.executor_fence("fence-garbled", "S1", store) is None, (
            "an unreadable manifest must not withhold work"
        )
    assert any(
        record.levelno >= 30 and "dispatch fence" in record.message
        for record in caplog.records
    ), "the unreadable-manifest case must be logged at WARN"


def test_executor_fence_aborts_for_a_foreign_live_lease(plan_dir):
    """Another live process owns this dispatch: refuse to spawn."""
    fence = _fence()
    _write_manifest(
        plan_dir,
        "fence-foreign",
        {
            "S1": _fresh_story(
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=_live_foreign_pid(),
            )
        },
    )
    store = _PathStore(plan_dir / "fence-foreign.manifest.json")

    result = fence.executor_fence("fence-foreign", "S1", store)

    assert result == {"ok": False, "error": "dispatch lease lost"}, result


def test_executor_fence_aborts_for_an_abandoned_worker(plan_dir):
    """An abandoned worker is refused even while it still owns the lease."""
    fence = _fence()
    _write_manifest(
        plan_dir,
        "fence-abandon",
        {
            "S1": _fresh_story(
                dispatch_lease_expires_at=_iso_in(600),
                dispatch_lease_owner_pid=os.getpid(),
            )
        },
    )
    store = _PathStore(plan_dir / "fence-abandon.manifest.json")
    box = {}

    def _run():
        fence.mark_worker_abandoned(threading.current_thread())
        box["result"] = fence.executor_fence("fence-abandon", "S1", store)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive(), "the fence thread did not finish"

    assert box.get("result") == {"ok": False, "error": "dispatch abandoned"}, (
        box.get("result")
    )
