"""Shared fixtures/helpers for the pipeline MCP server test suite, split
across test_pipeline_mcp_server_*.py files (originally one 16,551-line
test_pipeline_mcp_server.py) to keep each file under the project's
line-count target.

Two tiers of helpers live here:
  - CORE (imported into every split file): the fixtures every test depends
    on (directly or via autouse) plus _write_manifest/_read_manifest, which
    are used from nearly every section of the original file.
  - CROSS-GROUP: helpers whose usage spans more than one split file's
    section boundary (_story, _fake_plane, _explode_plane,
    _NullSetStateProvider, _STEP_CAP_MARKER_*, _make_fake_git_run,
    _FakeProc, _RATE_LIMIT_MSG, _FakePlannerBackend), imported only by the
    split files that actually need them.

Helpers used within a single section (e.g. _resume_rebase_harness,
_setup_conflict_repo, _css_setup, _make_story, _setup_oracle_story) stay
in their own split file rather than moving here.
"""
import json
import subprocess

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage

# ---------- CORE: fixtures + manifest helpers (needed everywhere) ----------


@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    # Reset the process-level Plane reachability verdict introduced in
    # test_ticketing_reachability.py's change: a prior test that found Plane
    # unreachable flips pt._plane_reachable to False, which would make every
    # later _plane_set_state call short-circuit and break retry/notify tests.
    pt._plane_reachable = None
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    """Default the test world to "Plane is wired up", which is what the
    existing tests assume (they mock plane_request and expect calls to
    happen). The Plane-optional path is exercised by the handful of tests
    that explicitly clear these to "" via _plane_disabled."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


@pytest.fixture
def _plane_disabled(monkeypatch):
    """Simulate an unconfigured Plane (no API key / workspace / project), so
    Plane calls must be skipped rather than fired at a dead endpoint."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")


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
    # pipeline_persona imports AGENTS_DIR from pipeline_paths at module load
    # and reads it as a free var, so patches must land on its own binding too.
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
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
def _isolate_usage_state(tmp_path, monkeypatch):
    # Point the usage gate at a non-existent tmp file for EVERY test so none of
    # them read the developer's live ~/.claude/usage_state.json. That file is
    # rewritten every ~60s by the real usage poller, so advance_pipeline tests
    # that don't otherwise stub the gate were flaky - passing or failing purely
    # on whether the live session/week usage happened to be over the pause
    # threshold when the suite ran. A missing file reads as "not paused".
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _hermetic_ollama_seams(monkeypatch):
    """Make the suite hermetic: stub every live-Ollama HTTP seam to a no-op.

    Three production paths fire real Ollama requests during tests that only
    mock the subprocess/Plane boundaries, and each is a multi-second
    /api/chat round-trip (or, on a cold model load, tens of seconds):

      - `_dispatch_story_impl` runs the guided-decomposition planner
        (`_run_planner` -> `get_backend("planner").complete()`, a 600s-timeout
        /api/chat) on every fresh local-family dispatch with no plan on disk.
      - `check_story_status` runs the step-cap diagnosis (`diagnose_failure`
        -> `get_backend("diagnosis").complete()`) on every step-cap exit.
      - every local dispatch probes `/api/tags` (`_ollama_loaded_models`) and
        serving parallelism (`_ollama_serving_parallelism`) as observability
        hooks that must never gate dispatch.

    None of these are what the tests in this file assert on - they assert
    routing/streak/journal/argv behavior and mock `get_backend` or
    `_run_planner` directly when they DO care about planner output. So stub
    the seams to fast no-ops here. Tests needing a specific value override
    with their own `monkeypatch.setattr`, which runs later on the same
    function-scoped monkeypatch and wins.

    `OllamaDriver.complete` is stubbed at the class rather than `p._run_planner`
    itself because several tests call `p._run_planner(...)` directly to pin its
    routing (they mock `backend.get_backend`, so they never reach the real
    driver); stubbing the chat method leaves those intact while short-
    circuiting the dispatch path that would otherwise hit a live server. No
    test in this file exercises the real `OllamaDriver.complete` chat behavior
    - that coverage lives in test_backend.py / test_acceptance_ollama_*.py.
    A "" response makes `_run_planner` return None (its documented fail-open),
    so dispatch proceeds with no checklist exactly like an unconfigured
    planner.
    """
    monkeypatch.setattr(backend.OllamaDriver, "complete",
                        lambda self, prompt, **kw: "")
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: set())
    monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: None)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)


@pytest.fixture
def usage_state_path(_isolate_usage_state):
    # Same isolated path as the autouse fixture; tests that want a specific gate
    # state write to it.
    return _isolate_usage_state


SAMPLE_USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 9% used · resets Jun 18 at 11:59am (America/Chicago)\n"
    "Current week (all models): 48% used · resets Jun 23 at 9am (America/Chicago)\n\n"
    "What's contributing to your limits usage?\n"
)


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


# ---------- CROSS-GROUP: used from more than one split file ----------


def _story(**over):
    base = {"summary": "Do the thing", "agent_instructions": "Build it with tests."}
    base.update(over)
    return base


def _fake_plane(method, path, **kwargs):
    if path.endswith("/states/"):
        return {"results": [
            {"group": "backlog", "id": "st-backlog"},
            {"group": "started", "id": "st-started"},
            {"group": "completed", "id": "st-done"},
        ]}
    if path.endswith("/labels/") and method == "GET":
        return {"results": []}
    if path.endswith("/labels/") and method == "POST":
        return {"id": "label-1"}
    if path.endswith("/epics/") and method == "POST":
        return {"id": "epic-1"}
    if path.endswith("/work-items/") and method == "POST":
        return {"id": "issue-1"}
    return {}


def _explode_plane(*a, **kw):
    raise AssertionError("plane_request must not be called when Plane is unconfigured")


class _NullSetStateProvider:
    """A no-op TicketProvider for mark_story_done completion-signal tests.

    Accepts set_state calls (so mark_story_done doesn't try to hit Plane) and
    returns True, mirroring the _FakeProvider pattern used by the existing
    ticket-provider routing tests.
    """

    def set_state(self, story_key, state, plan_name=None):
        return True


_STEP_CAP_MARKER_LOCAL = "[ended without done — step cap reached]"
_STEP_CAP_MARKER_ORACLE = "[ended without oracle green — step cap reached]"


def _make_fake_git_run(head_sha="deadbeef"):
    """Return a subprocess.run stub that mimics _commit_wip's git usage:
    `git add -A` (ok), `git reset -q -- agent.log` (ok), `git commit`
    (ok, no-op), `git rev-parse HEAD` (returns head_sha)."""
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = f"{head_sha}\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    return _fake_run


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


_RATE_LIMIT_MSG = (
    "You've hit your session limit · resets 8:20pm (America/Chicago)"
)


class _FakePlannerBackend:
    """Stand-in for whatever backend.get_backend(...) returns, capturing the
    exact kwargs _run_planner passed to complete()."""
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeTestAuthorBackend:
    def __init__(self, pid, raises=None):
        self.pid = pid
        self.raises = raises
        self.calls = []

    def dispatch(self, prompt, *, system, model, allowed_tools, cwd, log_path, append):
        self.calls.append({
            "prompt": prompt, "system": system, "model": model,
            "allowed_tools": allowed_tools, "cwd": cwd, "log_path": log_path,
            "append": append,
        })
        if self.raises:
            raise self.raises
        return backend.AgentHandle(pid=self.pid)


def _already_reaped_pid():
    """A pid that is guaranteed dead and already reaped, for tests that
    don't care about real dispatch timing - _wait_for_agent_exit treats
    this identically to "the agent exited"."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _make_worktree_repo(tmp_path, branch):
    """Real git repo + worktree on `branch` off base branch "main", no
    commits yet on `branch` beyond the shared base - lets tests exercise
    the real _worktree_has_new_commits check without mocking git."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   capture_output=True, text=True, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(wt)],
                   cwd=repo, capture_output=True, text=True, check=True)
    return repo, wt


__all__ = [
    "SAMPLE_USAGE_TEXT",
    "_RATE_LIMIT_MSG",
    "_STEP_CAP_MARKER_LOCAL",
    "_STEP_CAP_MARKER_ORACLE",
    "_FakePlannerBackend",
    "_FakeProc",
    "_FakeTestAuthorBackend",
    "_NullSetStateProvider",
    "_already_reaped_pid",
    "_clear_caches",
    "_explode_plane",
    "_fake_plane",
    "_hermetic_ollama_seams",
    "_isolate_usage_state",
    "_make_fake_git_run",
    "_make_worktree_repo",
    "_plane_configured",
    "_plane_disabled",
    "_read_manifest",
    "_story",
    "_write_manifest",
    "agents_dir",
    "plan_dir",
    "usage_state_path",
    "worktree_root",
]
