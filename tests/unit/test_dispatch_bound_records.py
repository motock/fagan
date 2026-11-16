"""The dispatch path reads and writes its records in the bound namespace.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so a manifest or journal the dispatch path reads or writes below a tick has to
land in the namespace bound for that tick.  Naming the default partition
instead sends the write to a partition the plan's own namespace never sees:
the scheduler reads the plan back from the bound partition and finds nothing.

These tests grade that through the public entry points of the four dispatch
modules:

- ``dispatch_fence.executor_fence`` re-reads the story from the record store
  before it lets an agent spawn, so it must see the bound partition.
- ``dispatch_worktree._refuse_if_remote_branch_taken`` persists the refusal
  through the record store, so it must write the bound partition.
- ``dispatch_phases._step_detached_test_author`` persists the phase state
  through the record store, so it must write the bound partition.
- ``dispatch_story`` (through ``pipeline.dispatch``) persists the manifest
  and reads the resume journal through the record store, so both must use the
  bound partition.

The last test is the regression guard the story asks for: no dispatch module
may name the default partition in a record-store call again, and each one must
reach for the shared ``bound_records`` wrappers.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

from pipeline import dispatch as pdispatch
from pipeline import dispatch_fence, dispatch_phases, dispatch_worktree
from pipeline import server as p
from pipeline import test_author as ptest_author
from pipeline.actor import bind_namespace
from pipeline.audit_memory import InMemoryAuditLog
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "S1"

#: The four modules the story names, each with the record-store call sites it
#: must not name the default partition on.
DISPATCH_MODULES = (
    "dispatch.py",
    "dispatch_phases.py",
    "dispatch_worktree.py",
    "dispatch_fence.py",
)

#: A record-store call that names the default partition literally.
_DEFAULT_PARTITION_CALL = re.compile(r'records\(\)\.\w+\(\s*"default"')


class _RecordingRecords:
    """A record view that records the namespace of every call it forwards."""

    def __init__(self, inner: InMemoryRecordStore) -> None:
        self._inner = inner
        self.manifest_writes: list[tuple[str, str]] = []
        self.journal_reads: list[tuple[str, str, str]] = []

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        self.manifest_writes.append((namespace, plan_name))
        self._inner.put_manifest(namespace, plan_name, manifest)

    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]:
        self.journal_reads.append((namespace, plan_name, story_key))
        return self._inner.get_journal(namespace, plan_name, story_key)

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        return self._inner.get_manifest(namespace, plan_name)

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        self._inner.append_journal(namespace, plan_name, story_key, record)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _Store:
    """A store facade whose record view is the recording one.

    The audit log is the in-memory one: the file layout's audit log serves the
    default partition only, and the file layout's refusal is not what these
    tests grade.
    """

    def __init__(self, inner, records: _RecordingRecords) -> None:
        self._inner = inner
        self._records = records
        self._audit_log = InMemoryAuditLog()

    def records(self) -> _RecordingRecords:
        return self._records

    def audit_log(self) -> InMemoryAuditLog:
        return self._audit_log

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.fixture
def records() -> _RecordingRecords:
    """The recording view over an in-memory record store.

    The counters live on the wrapper, so the fixture hands out the wrapper
    itself: the ``store`` fixture wraps this same object, and a test that reads
    ``records.manifest_writes`` sees the calls the dispatch path made.
    """
    return _RecordingRecords(InMemoryRecordStore())


@pytest.fixture
def store(monkeypatch, records: _RecordingRecords) -> _Store:
    """Install the recording store at the live ``pipeline.server`` binding.

    Every dispatch module resolves ``_store`` through a ``_ServerRef``, so
    patching the one server binding reaches all of them.
    """
    facade = _Store(p._store, records)
    monkeypatch.setattr(p, "_store", facade, raising=False)
    return facade


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point the plan directory at a scratch dir so the plan lock can open."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    from pipeline import persistence as ppers

    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def _manifest(stories: dict | None = None) -> dict:
    return {"epics": {}, "stories": stories if stories is not None else {}}


# -- dispatch_fence: the story is re-read from the bound partition ----------


def _fence_store(records: InMemoryRecordStore, manifest: dict) -> _Store:
    """A store facade whose record view holds *manifest* in one partition."""
    inner = _RecordingRecords(records)
    return _Store(object(), inner)


def test_fence_reads_the_story_from_the_bound_partition(records) -> None:
    """A terminal story in the bound partition stops the spawn.

    The fence re-reads the story under the plan lock; if it read another
    partition it would see an empty story and let the agent spawn on a story
    that is already done.
    """
    records.put_manifest(
        NAMESPACE, PLAN, _manifest({STORY: {"status": "done", "summary": "x"}})
    )
    store = _fence_store(records, None)

    with bind_namespace(NAMESPACE):
        result = dispatch_fence.executor_fence(PLAN, STORY, store)

    assert result == {"ok": False, "error": "dispatch lease lost"}


def test_fence_ignores_a_terminal_story_in_another_partition(records) -> None:
    """A terminal story in the default partition does not stop the spawn.

    The bound partition is the one that counts: a story that only the default
    partition knows about is not the story this tick is dispatching.
    """
    records.put_manifest(
        DEFAULT_NAMESPACE, PLAN, _manifest({STORY: {"status": "done"}})
    )
    store = _fence_store(records, None)

    with bind_namespace(NAMESPACE):
        result = dispatch_fence.executor_fence(PLAN, STORY, store)

    assert result is None


def test_fence_reads_the_default_partition_when_nothing_is_bound(records) -> None:
    """With no namespace bound the fence keeps reading the default partition."""
    records.put_manifest(
        DEFAULT_NAMESPACE, PLAN, _manifest({STORY: {"status": "done"}})
    )
    store = _fence_store(records, None)

    assert dispatch_fence.executor_fence(PLAN, STORY, store) == {
        "ok": False,
        "error": "dispatch lease lost",
    }


def test_fence_proceeds_when_the_bound_partition_holds_no_story(records) -> None:
    """An empty bound partition yields no story, so the fence proceeds."""
    store = _fence_store(records, None)

    with bind_namespace(NAMESPACE):
        assert dispatch_fence.executor_fence(PLAN, STORY, store) is None


# -- dispatch_worktree: the refusal is persisted in the bound partition ------


def _repo_root_factory(repo_root: Path):
    """Stand in for ``dispatch._scoped_repo_root``: a plan-name factory."""

    @contextmanager
    def _factory(plan_name: str):
        yield repo_root

    return _factory


def _patch_remote_branch_taken(monkeypatch, repo_root: Path) -> None:
    """Make the fresh-dispatch remote branch guard refuse."""
    monkeypatch.setattr(
        pdispatch, "_scoped_repo_root", _repo_root_factory(repo_root), raising=False
    )
    monkeypatch.setattr(
        pdispatch, "remote_branch_exists", lambda repo, branch: True, raising=False
    )
    monkeypatch.setattr(pdispatch, "_notify_user", lambda *a, **k: None, raising=False)


def test_worktree_refusal_writes_the_manifest_to_the_bound_partition(
    store, records, tmp_path, monkeypatch
) -> None:
    """A blocked fresh dispatch persists the refusal in the bound partition."""
    _patch_remote_branch_taken(monkeypatch, tmp_path)
    manifest = _manifest({STORY: {"status": "todo"}})

    with bind_namespace(NAMESPACE):
        result = dispatch_worktree._refuse_if_remote_branch_taken(
            PLAN, STORY, manifest["stories"][STORY], "story/some-branch", manifest
        )

    assert result is not None and result.get("ok") is False
    assert records.get_manifest(NAMESPACE, PLAN) == manifest
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None
    assert records.get_manifest(OTHER_NAMESPACE, PLAN) is None


def test_worktree_refusal_writes_the_default_partition_when_unbound(
    store, records, tmp_path, monkeypatch
) -> None:
    """With nothing bound the refusal still lands in the default partition."""
    _patch_remote_branch_taken(monkeypatch, tmp_path)
    manifest = _manifest({STORY: {"status": "todo"}})

    dispatch_worktree._refuse_if_remote_branch_taken(
        PLAN, STORY, manifest["stories"][STORY], "story/some-branch", manifest
    )

    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) == manifest
    assert records.get_manifest(NAMESPACE, PLAN) is None


# -- dispatch_phases: the phase state is persisted in the bound partition ---


def _patch_detached_phase(monkeypatch, mode: str) -> None:
    """Enable the detached test-author phase and fake the phase runner.

    ``mode`` selects which branch of the phase runs: ``authored`` and
    ``fell_open`` are the two outcomes of a re-entry, ``launched`` is the
    first entry that starts the phase.
    """
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    monkeypatch.setattr(p, "_LOCAL_BACKEND_NAMES", ("local",), raising=False)
    monkeypatch.setattr(
        ptest_author,
        "collect_test_author_phase",
        lambda phase, **kwargs: mode == "authored",
        raising=False,
    )
    monkeypatch.setattr(
        ptest_author,
        "start_test_author_phase",
        lambda *args, **kwargs: {"pid": 1} if mode == "launched" else None,
        raising=False,
    )


def _run_detached_phase(store, tmp_path, monkeypatch, mode: str):
    """Run one detached test-author step; returns its result string."""
    _patch_detached_phase(monkeypatch, mode)
    story = {"summary": "s", "status": "todo"}
    if mode in ("authored", "fell_open"):
        story["test_author_phase"] = {"pid": 1}
    manifest = _manifest({STORY: story})
    return dispatch_phases._step_detached_test_author(
        story,
        story_key=STORY,
        worktree_path=tmp_path / "wt",
        dispatch_backend="local",
        local_model="local-model",
        plan_name=PLAN,
        plan_role_config=None,
        manifest=manifest,
        marker_path=tmp_path / "marker",
        resuming=False,
    )


@pytest.mark.parametrize("mode", ["authored", "fell_open", "launched"])
def test_phase_persists_the_manifest_in_the_bound_partition(
    store, records, tmp_path, monkeypatch, mode
) -> None:
    """Every phase outcome persists the manifest in the bound partition."""
    with bind_namespace(NAMESPACE):
        result = _run_detached_phase(store, tmp_path, monkeypatch, mode)

    assert result in ("authored", "fell_open", "pending")
    assert records.get_manifest(NAMESPACE, PLAN) is not None
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None
    assert records.get_manifest(OTHER_NAMESPACE, PLAN) is None


def test_phase_persists_the_default_partition_when_unbound(
    store, records, tmp_path, monkeypatch
) -> None:
    """With nothing bound the phase still persists to the default partition."""
    result = _run_detached_phase(store, tmp_path, monkeypatch, "authored")

    assert result == "authored"
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is not None
    assert records.get_manifest(NAMESPACE, PLAN) is None


# -- dispatch: the manifest writes and the journal read use the bound one ---


@pytest.fixture
def dispatch_env(tmp_path, monkeypatch):
    """Wire the dispatch seams so a dispatch reaches the record store."""
    from app import backend
    from pipeline import paths as ppaths
    from pipeline import persistence as ppers
    from pipeline import ticketing as pt

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    for mod in (p, ppers, ppaths):
        monkeypatch.setattr(mod, "PLAN_DIR", plans)
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 1})()
    )
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane"))
    )
    monkeypatch.setattr(pdispatch, "_default_branch", lambda: "main")
    monkeypatch.setattr(pdispatch, "_notify_user", lambda *a, **k: None)
    return worktrees


def _write_plan_manifest(dispatch_env, story_key: str = STORY) -> None:
    """Write the plan manifest the dispatch reads."""
    manifest = _manifest(
        {
            story_key: {
                "summary": "x",
                "agent_instructions": "BRIEF-TEXT",
                "status": "todo",
                "dependencies": [],
            }
        }
    )
    (dispatch_env.parent / "plans" / f"{PLAN}.manifest.json").write_text(
        json.dumps(manifest)
    )


def test_dispatch_reads_the_resume_journal_from_the_bound_partition(
    store, records, dispatch_env, monkeypatch
) -> None:
    """The resume journal comes from the bound partition."""
    _write_plan_manifest(dispatch_env)
    (dispatch_env / STORY).mkdir()
    records.append_journal(NAMESPACE, PLAN, STORY, {"step": "s1"})

    with bind_namespace(NAMESPACE):
        result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert records.journal_reads == [(NAMESPACE, PLAN, STORY)]


def test_dispatch_writes_the_manifest_to_the_bound_partition(
    store, records, dispatch_env, monkeypatch
) -> None:
    """Every manifest the dispatch persists lands in the bound partition."""
    _write_plan_manifest(dispatch_env)
    (dispatch_env / STORY).mkdir()

    with bind_namespace(NAMESPACE):
        result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert records.manifest_writes, "the dispatch persisted no manifest"
    assert {namespace for namespace, _ in records.manifest_writes} == {NAMESPACE}
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


def test_dispatch_writes_the_manifest_to_the_default_partition_when_unbound(
    store, records, dispatch_env, monkeypatch
) -> None:
    """With nothing bound the dispatch still persists to the default partition."""
    _write_plan_manifest(dispatch_env)
    (dispatch_env / STORY).mkdir()

    result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert records.manifest_writes, "the dispatch persisted no manifest"
    assert {namespace for namespace, _ in records.manifest_writes} == {
        DEFAULT_NAMESPACE
    }


# -- the regression guard ---------------------------------------------------


@pytest.mark.parametrize("module_name", DISPATCH_MODULES)
def test_dispatch_module_never_names_the_default_partition(module_name: str) -> None:
    """No dispatch module may name the default partition in a record call.

    A literal ``"default"`` in a record-store call is the defect this story
    removes; it is the regression guard against it creeping back.
    """
    source = (Path(pdispatch.__file__).parent / module_name).read_text()

    assert not _DEFAULT_PARTITION_CALL.search(source), (
        f"pipeline/{module_name} names the default partition in a record-store "
        "call; it must select the namespace bound for the call instead"
    )


@pytest.mark.parametrize("module_name", DISPATCH_MODULES)
def test_dispatch_module_uses_the_bound_records_wrappers(module_name: str) -> None:
    """Each dispatch module reaches for the shared bound-namespace wrappers."""
    source = (Path(pdispatch.__file__).parent / module_name).read_text()

    assert "bound_records" in source, (
        f"pipeline/{module_name} must select the namespace through the "
        "pipeline.bound_records wrappers"
    )
