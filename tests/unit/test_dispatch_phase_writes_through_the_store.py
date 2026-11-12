"""The dispatch-phase manifest writers persist through the record store.

Every write site listed in story 0e5974e0 runs inside the plan transaction its
caller already holds, so each one must hand the manifest it is mutating to
``_store.records().put_manifest("default", plan_name, manifest)`` — never the
module-local ``_atomic_write_json(manifest_path, manifest)`` seam, which writes
the file projection behind the store's back (and, under ``PIPELINE_STORE=sqlite``,
bypasses the database row entirely).

The fake store below records every put and rejects the plan names the real
store rejects, so a site that swallows the error (or falls back to the file
writer) fails here.
"""

import json
from contextlib import contextmanager

import pytest

from pipeline import (
    dispatch,
    dispatch_context,
    dispatch_phases,
    dispatch_worktree,
    no_progress,
)
from pipeline import server as p
from pipeline import test_author as ptest_author
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name


class _FakeRecords:
    """Record view stand-in: validates the key like the real store, then
    records the manifest it was handed."""

    def __init__(self, store: "_FakeStore") -> None:
        self._store = store

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        validate_plan_name(plan_name)
        self._store.calls.append((namespace, plan_name, manifest))
        self._store.persisted[(namespace, plan_name)] = manifest


class _FakeStore:
    """Minimal ``_store`` stand-in that captures ``put_manifest`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.persisted: dict[tuple[str, str], dict] = {}
        self._records = _FakeRecords(self)

    def records(self) -> _FakeRecords:
        return self._records


@pytest.fixture
def fake_store(monkeypatch):
    """Install a fake ``_store`` at the live pipeline.server binding every
    module's ``_ServerRef("_store")`` resolves through, and record any call
    that still goes to the file writer."""
    store = _FakeStore()
    file_writes: list[tuple[object, dict]] = []
    monkeypatch.setattr(p, "_store", store, raising=False)
    monkeypatch.setattr(
        p, "_atomic_write_json", lambda path, data: file_writes.append((path, data)),
        raising=False,
    )
    store.file_writes = file_writes
    return store


def _log(worktree, *done_lines):
    body = "[step 1] bash: pytest -q\n"
    for i, line in enumerate(done_lines, start=4):
        body += f"[step {i}] DONE: {line}\n"
    (worktree / "agent.log").write_text(body)


def _no_progress_case(tmp_path, monkeypatch, fake_store, log, done_text=None):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if done_text is None:
        (worktree / "agent.log").write_text(log)
    else:
        _log(worktree, done_text)
    story = {"summary": "s", "status": "tests_passed", "rework_attempts": 0}
    manifest = {"epics": {}, "stories": {"S1": story}}
    path = tmp_path / "plan.manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        no_progress.usage_cooldown, "trip", lambda *a, **k: None
    )
    monkeypatch.setattr(
        no_progress.usage_transitions, "announce", lambda *a, **k: None
    )
    return story, manifest, path, str(worktree)


def test_should_persist_wait_for_capacity_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """_wait_for_capacity: a usage-cap death redispatch records the bumped
    capacity_waits counter through the store, not the file writer."""
    case = _no_progress_case(
        tmp_path, monkeypatch, fake_store,
        "[boot] model=x\nClaude usage limit reached. Reset at 3pm.\n",
    )
    story, manifest, _path, worktree = case

    result = no_progress.classify_no_progress(
        "wait-plan", "S1", story, worktree, manifest
    )

    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert story["capacity_waits"] == 1
    assert fake_store.persisted == {("default", "wait-plan"): manifest}
    assert fake_store.file_writes == []


def test_should_persist_retry_transport_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """_retry_transport: a pre-tool transport death records the bumped
    transport_retries counter through the store."""
    case = _no_progress_case(
        tmp_path, monkeypatch, fake_store,
        "[boot] model=x\nLLM call failed: ConnectError: connection refused\n",
    )
    story, manifest, _path, worktree = case

    result = no_progress.classify_no_progress(
        "retry-plan", "S1", story, worktree, manifest
    )

    assert result == {"status": "changes_requested", "reason": "transport_failure"}
    assert story["transport_retries"] == 1
    assert fake_store.persisted == {("default", "retry-plan"): manifest}
    assert fake_store.file_writes == []


def test_should_persist_scope_conflict_park_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """classify_no_progress: a reported SCOPE-CONFLICT park is recorded
    through the store."""
    case = _no_progress_case(
        tmp_path, monkeypatch, fake_store, "",
        done_text="SCOPE-CONFLICT: pipeline/a.py :: needs a change here",
    )
    story, manifest, _path, worktree = case

    result = no_progress.classify_no_progress(
        "conflict-plan", "S1", story, worktree, manifest
    )

    assert result == {"status": "parked", "reason": "scope_conflict"}
    assert story["status"] == "parked"
    assert fake_store.persisted == {("default", "conflict-plan"): manifest}
    assert fake_store.file_writes == []


def _source(uri, name="ux", required=True):
    return {
        "name": name, "provider": "dir", "uri": str(uri), "phases": ["dispatch"],
        "mode": "prompt", "max_chars": 20000, "authority": "reference",
        "priority": 0, "required": required,
    }


def test_should_persist_context_pin_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """_step_resolve_context: a required context source that fails pins the
    story's dispatch_block_reason through the store."""
    manifest = {
        "context": {"sources": [_source(tmp_path / "missing-dir")]},
        "stories": {"S1": {}},
    }
    story = {"context_digests": {}}

    block, blocked = dispatch_context._step_resolve_context(
        "context-plan", "S1", story, manifest
    )

    assert block == ""
    assert blocked is not None and blocked["ok"] is False
    assert "dispatch_block_reason" in story
    assert fake_store.persisted == {("default", "context-plan"): manifest}
    assert fake_store.file_writes == []


def test_should_persist_phase_transition_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """_step_detached_test_author: the authored transition is recorded through
    the store."""
    monkeypatch.setattr(
        dispatch_phases, "_test_author_detached_enabled", lambda: True
    )
    monkeypatch.setattr(
        ptest_author, "collect_test_author_phase", lambda *a, **k: True
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    marker = worktree / ".tdd_split_test_author_done"
    story = {"test_author_phase": {"pid": 1}}
    manifest = {"stories": {"S1": story}}

    outcome = dispatch_phases._step_detached_test_author(
        story,
        story_key="S1",
        worktree_path=worktree,
        dispatch_backend="ollama",
        local_model="local-model",
        plan_name="phase-plan",
        plan_role_config=None,
        manifest=manifest,
        marker_path=marker,
        resuming=True,
    )

    assert outcome == "authored"
    assert story["tdd_split"] is True
    assert fake_store.persisted == {("default", "phase-plan"): manifest}
    assert fake_store.file_writes == []


def _install_worktree_guard(monkeypatch, tmp_path):
    @contextmanager
    def scoped_repo_root(_plan_name):
        yield tmp_path

    monkeypatch.setattr(dispatch, "_scoped_repo_root", scoped_repo_root)
    monkeypatch.setattr(
        dispatch, "remote_branch_exists", lambda root, branch: True
    )
    monkeypatch.setattr(dispatch, "_notify_user", lambda *a, **k: None)


def test_should_persist_worktree_record_through_the_record_store(
    tmp_path, monkeypatch, fake_store
):
    """_refuse_if_remote_branch_taken: the branch-guard refusal records the
    story's dispatch_block_reason through the store."""
    _install_worktree_guard(monkeypatch, tmp_path)
    story: dict = {}
    manifest = {"stories": {"S1": story}}

    block = dispatch_worktree._refuse_if_remote_branch_taken(
        "branch-plan", "S1", story, "agent/setup-01", manifest
    )

    assert block is not None
    assert "dispatch_block_reason" in story
    assert fake_store.persisted == {("default", "branch-plan"): manifest}
    assert fake_store.file_writes == []


def test_should_not_write_for_a_plan_name_the_store_rejects(
    tmp_path, monkeypatch, fake_store
):
    """A plan name the store refuses fails closed: the InvalidRecordKeyError
    propagates, nothing is written, and there is no file-writer fallback."""
    _install_worktree_guard(monkeypatch, tmp_path)
    story: dict = {}
    manifest = {"phase": "wait", "capacity": 3}

    with pytest.raises(InvalidRecordKeyError):
        dispatch_worktree._refuse_if_remote_branch_taken(
            "a..b", "S1", story, "agent/setup-01", manifest
        )

    assert isinstance(InvalidRecordKeyError("x"), ValueError)
    assert fake_store.persisted == {}
    assert fake_store.file_writes == []

    good_story: dict = {}
    good_manifest = {"phase": "wait", "capacity": 4}
    dispatch_worktree._refuse_if_remote_branch_taken(
        "good-plan", "S1", good_story, "agent/setup-01", good_manifest
    )

    assert fake_store.persisted == {
        ("default", "good-plan"): {"phase": "wait", "capacity": 4}
    }
    assert ("default", "a..b") not in fake_store.persisted
    assert fake_store.file_writes == []
