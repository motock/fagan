"""dispatch_story's own manifest writes persist through the record store.

``pipeline/dispatch.py``'s ``_dispatch_story_impl`` opens the plan transaction
itself (``with _store.transaction(plan_name) as acquired:``) and mutates the
manifest it read inside that scope at four points:

* the initial claim / brief write (branch + correlation ID) before any
  worktree, git or agent work,
* the rebase-conflict park write,
* the blocked-oracle gate write,
* the completed-dispatch write (pid, dispatched_at, worktree, log, model,
  and the agent_instructions snapshot).

Each one must hand the manifest to
``_store.records().put_manifest("default", plan_name, manifest)`` — never the
module-local ``_atomic_write_json(manifest_path, manifest)`` file seam, which
writes the file projection behind the store's back (and, under
``PIPELINE_STORE=sqlite``, bypasses the database row entirely).

The recording store below wraps the real ``FileStore`` so every test drives
the real dispatch path (real transaction, real key validation) and simply
observes what the store was asked to persist.
"""

import copy
import json
import subprocess

import pytest

from app import backend
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name

PLAN = "dwstore"
STORY = "S1"

_BASE_STORY = {
    "summary": "Do thing",
    "agent_instructions": "Build it.",
    "status": "todo",
    "dependencies": [],
}


# ---------------------------------------------------------------------------
# Harness


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _write_manifest(plans, plan_name, stories):
    (plans / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _manifest_bytes(plans, plan_name):
    return (plans / f"{plan_name}.manifest.json").read_bytes()


def _dispatch_env(tmp_path, monkeypatch):
    """Point PLAN_DIR / WORKTREE_ROOT / AGENTS_DIR at throwaway dirs."""
    plans = tmp_path / "plans"
    plans.mkdir()
    for mod in (p, ppers):
        monkeypatch.setattr(mod, "PLAN_DIR", plans)

    wts = tmp_path / "worktrees"
    wts.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wts)

    agents = tmp_path / "agents"
    agents.mkdir()
    for name, model in (
        ("overlord", "opus"),
        ("software-engineer", "sonnet"),
        ("code-reviewer", "sonnet"),
        ("product-analyst", "opus"),
    ):
        (agents / f"{name}.md").write_text(
            f'---\nname: "{name}"\nmodel: {model}\n---\n\nBody of {name}.\n'
        )
    monkeypatch.setattr(p, "AGENTS_DIR", agents)
    monkeypatch.setattr(pper, "AGENTS_DIR", agents)

    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")
    return plans, wts


def _stub_dispatch_externals(monkeypatch, behind=0):
    """Stub git / subprocess / Plane / default-branch at their boundaries."""

    def _fake_run(cmd, cwd=None, **kwargs):
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, str(behind), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        backend.subprocess, "Popen", lambda cmd, env=None, **kw: _FakeProc(4242)
    )
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


def _resume_setup(plans, wts, monkeypatch, behind, rebase_result):
    """Seed a *resumed* dispatch: an existing worktree whose base is ``behind``
    commits behind origin/<default>, and a canned rebase outcome."""
    _write_manifest(
        plans,
        PLAN,
        {
            STORY: {
                "summary": "Do thing",
                "agent_instructions": "Build it.",
                "status": "interrupted",
                "dependencies": [],
            }
        },
    )
    wt = wts / STORY
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /fake\n")

    _stub_dispatch_externals(monkeypatch, behind=behind)
    monkeypatch.setattr(
        p, "_rebase_onto_master", lambda wt_arg, br: dict(rebase_result)
    )
    monkeypatch.setattr(p, "_sync_branch_remote", lambda *a, **k: {"ok": True})
    return wt


# ---------------------------------------------------------------------------
# The recording store


class _RecordingRecords:
    """Delegating record view that remembers every ``put_manifest``."""

    def __init__(self, store: "_RecordingStore") -> None:
        self._store = store

    def get_manifest(self, namespace, plan_name):
        return self._store.inner.records().get_manifest(namespace, plan_name)

    def get_manifests(self, namespace):
        return self._store.inner.records().get_manifests(namespace)

    def put_manifest(self, namespace, plan_name, manifest):
        # Mirror the real FileRecords.put_manifest: an unsafe plan name (one
        # parsers._validate_key accepts but the record layer rejects) raises
        # before anything is stored.
        validate_plan_name(plan_name)
        self._store.calls.append((namespace, plan_name, copy.deepcopy(manifest)))
        self._store.inner.records().put_manifest(namespace, plan_name, manifest)


class _RecordingStore:
    """``FileStore`` stand-in that observes manifest writes.

    Everything but ``records()`` delegates to the real store, so the dispatch
    path exercises the real transaction, the real manifest read and the real
    on-disk write.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str, dict]] = []
        self._records = _RecordingRecords(self)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def records(self):
        return self._records


class _BusyStore(_RecordingStore):
    """A store whose plan transaction is already held by someone else."""

    def transaction(self, plan_name, wait_seconds=0.0):
        return _busy_transaction()


class _busy_transaction:
    def __enter__(self):
        return False

    def __exit__(self, *exc):
        return False


@pytest.fixture
def store(monkeypatch):
    recording = _RecordingStore(p._store)
    monkeypatch.setattr(p, "_store", recording, raising=False)
    return recording


# ---------------------------------------------------------------------------
# 1. the initial claim / brief write


def test_initial_claim_write_goes_through_the_store(
    tmp_path, monkeypatch, store
):
    """The first write (branch + correlation ID, persisted before any
    worktree/git work) reaches the store, not the file writer."""
    plans, _wts = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plans, PLAN, {STORY: dict(_BASE_STORY)})
    _stub_dispatch_externals(monkeypatch)
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)

    result = p.dispatch_story(PLAN, STORY)

    assert result["ok"] is True
    assert store.calls, "dispatch never wrote the manifest through the store"
    namespace, plan_name, manifest = store.calls[0]
    assert (namespace, plan_name) == ("default", PLAN)
    story = manifest["stories"][STORY]
    assert story["correlation_id"], "the claim write must carry the correlation ID"
    assert story["branch"], "the claim write must carry the recorded branch"


# ---------------------------------------------------------------------------
# 2. the rebase-conflict park write


def test_parked_manifest_write_goes_through_the_store(tmp_path, monkeypatch, store):
    """A resume whose rebase conflicts parks the story and persists that
    status through the store (and nothing is launched)."""
    plans, wts = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _resume_setup(
        plans,
        wts,
        monkeypatch,
        behind=3,
        rebase_result={
            "ok": False,
            "conflict": True,
            "error": "non-additive conflict in src/app.py",
            "auto_resolved": False,
        },
    )

    result = p.dispatch_story(PLAN, STORY)

    assert result["status"] == "parked"
    assert len(store.calls) == 2, store.calls
    namespace, plan_name, manifest = store.calls[-1]
    assert (namespace, plan_name) == ("default", PLAN)
    story = manifest["stories"][STORY]
    assert story["status"] == "parked"
    assert "rebase conflict" in story["parked_reason"]


# ---------------------------------------------------------------------------
# 3. the blocked-oracle gate write


def test_blocked_oracle_write_goes_through_the_store(tmp_path, monkeypatch, store):
    """An unusable acceptance oracle blocks the story before launch and
    persists the verdict through the store."""
    plans, _wts = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plans, PLAN, {STORY: dict(_BASE_STORY)})
    _stub_dispatch_externals(monkeypatch)
    monkeypatch.setattr(
        p,
        "validate_acceptance_fixtures",
        lambda story, path: {"state": "errors", "detail": "broken fixture"},
    )

    result = p.dispatch_story(PLAN, STORY)

    assert result["status"] == "blocked_oracle"
    assert len(store.calls) == 2, store.calls
    namespace, plan_name, manifest = store.calls[-1]
    assert (namespace, plan_name) == ("default", PLAN)
    story = manifest["stories"][STORY]
    assert story["status"] == "blocked_oracle"
    assert story["oracle_gate"]["state"] == "errors"


# ---------------------------------------------------------------------------
# 4. the completed-dispatch write


def test_completed_dispatch_write_goes_through_the_store(
    tmp_path, monkeypatch, store
):
    """The final write stamps the launched agent's facts on the story and
    reaches the store last."""
    plans, _wts = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plans, PLAN, {STORY: dict(_BASE_STORY)})
    _stub_dispatch_externals(monkeypatch)
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)

    result = p.dispatch_story(PLAN, STORY)

    assert result["ok"] is True
    assert len(store.calls) >= 2, store.calls
    namespace, plan_name, manifest = store.calls[-1]
    assert (namespace, plan_name) == ("default", PLAN)
    story = manifest["stories"][STORY]
    assert story["status"] == "in_progress"
    assert story["pid"] == 4242
    assert story["worktree"]
    assert story["log"]
    assert story["dispatched_at"]
    assert story["_dispatched_agent_instructions"] == "Build it."


# ---------------------------------------------------------------------------
# 5. the existing early return when the transaction is busy


def test_should_not_write_when_the_transaction_is_busy(tmp_path, monkeypatch):
    """Another dispatch/interrupt already holds the plan transaction: the
    entry point returns the ``skipped: locked`` refusal and mutates nothing."""
    plans, _wts = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plans, PLAN, {STORY: dict(_BASE_STORY)})
    _stub_dispatch_externals(monkeypatch)
    before = _manifest_bytes(plans, PLAN)

    busy = _BusyStore(p._store)
    monkeypatch.setattr(p, "_store", busy, raising=False)

    result = p.dispatch_story(PLAN, STORY)

    assert result["skipped"] == "locked"
    assert busy.calls == []
    assert _manifest_bytes(plans, PLAN) == before


# ---------------------------------------------------------------------------
# 6. negative: a plan name the record layer rejects fails closed


class _KeyedRecords:
    """In-memory record view that validates plan names like the real one."""

    def __init__(self, manifest, calls):
        self._manifest = manifest
        self._calls = calls

    def get_manifest(self, namespace, plan_name):
        return self._manifest

    def get_manifests(self, namespace):
        return {"a..b": self._manifest}

    def put_manifest(self, namespace, plan_name, manifest):
        validate_plan_name(plan_name)
        self._calls.append((namespace, plan_name, manifest))


class _KeyedStore:
    """Fake store whose writes are validated, for the fail-closed case."""

    def __init__(self, manifest):
        self.calls: list[tuple[str, str, dict]] = []
        self._manifest = manifest
        self._records = _KeyedRecords(manifest, self.calls)

    def transaction(self, plan_name, wait_seconds=0.0):
        return _busy_transaction_true()

    def manifest_path(self, plan_name):
        return None

    def get_manifest(self, plan_name):
        return self._manifest

    def records(self):
        return self._records


class _busy_transaction_true:
    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


def test_invalid_plan_name_fails_closed(tmp_path, monkeypatch):
    """``a..b`` passes the path-traversal guard but the record layer rejects
    it: the write must raise InvalidRecordKeyError (a ValueError) and never
    reach the store."""
    _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _stub_dispatch_externals(monkeypatch)

    fake = _KeyedStore({"epics": {}, "stories": {STORY: dict(_BASE_STORY)}})
    monkeypatch.setattr(p, "_store", fake, raising=False)

    with pytest.raises(InvalidRecordKeyError):
        p.dispatch_story("a..b", STORY)

    assert fake.calls == []
