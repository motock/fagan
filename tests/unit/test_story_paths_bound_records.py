"""The story-path modules read and write their records in the bound namespace.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so a manifest or journal that a story path reads or writes below a tick has to
land in the namespace bound for that tick.  Naming the default partition
instead sends the write to a partition the plan's own namespace never sees:
the scheduler reads the plan back from the bound partition and finds nothing.

These tests grade that through the public entry points of the seven modules
this story names - ``story_status``, ``checkpoint``, ``no_progress``,
``triage``, ``triage_scope_actions``, ``plan_conflict_ruling`` and
``escalation`` - and end with the regression guard: no one of them may name
the default partition in a record-store call again, and each one must reach
for the shared ``bound_records`` wrappers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline import (
    checkpoint,
    escalation,
    no_progress,
    plan_conflict_ruling,
    story_status,
    triage,
    triage_scope_actions,
)
from pipeline.actor import bind_namespace
from pipeline import server as p

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "S1"

#: The seven modules the story names, each with the record-store call sites it
#: must not name the default partition on.
STORY_PATH_MODULES = (
    "story_status.py",
    "checkpoint.py",
    "no_progress.py",
    "triage.py",
    "triage_scope_actions.py",
    "plan_conflict_ruling.py",
    "escalation.py",
)

#: A record-store call that names the default partition literally.
_DEFAULT_PARTITION_CALL = re.compile(r'records\(\)\.\w+\(\s*"default"')

#: The import of the shared wrappers, in either of the two shapes that name
#: the module: ``from . import bound_records`` or ``from .bound_records
#: import ...``.
_BOUND_RECORDS_IMPORT = re.compile(
    r"from\s+\.\s+import\s+[^\n]*\bbound_records\b"
    r"|from\s+\.bound_records\s+import\b"
)


class _RecordingRecords:
    """A record view that records the namespace of every call it forwards."""

    def __init__(self) -> None:
        self.manifest_writes: list[tuple[str, str]] = []
        self.manifest_reads: list[tuple[str, str]] = []
        self.journal_writes: list[tuple[str, str, str]] = []
        self.journal_reads: list[tuple[str, str, str]] = []
        self.journal_clears: list[tuple[str, str, str]] = []
        self.manifests: dict[tuple[str, str], dict] = {}

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        self.manifest_writes.append((namespace, plan_name))
        self.manifests[(namespace, plan_name)] = manifest

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        self.manifest_reads.append((namespace, plan_name))
        return self.manifests.get((namespace, plan_name))

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        self.journal_writes.append((namespace, plan_name, story_key))

    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]:
        self.journal_reads.append((namespace, plan_name, story_key))
        return []

    def clear_journal(self, namespace: str, plan_name: str, story_key: str) -> None:
        self.journal_clears.append((namespace, plan_name, story_key))


class _Store:
    """A store facade whose record view is the recording one."""

    def __init__(self, records: _RecordingRecords) -> None:
        self._records = records

    def records(self) -> _RecordingRecords:
        return self._records


@pytest.fixture
def records() -> _RecordingRecords:
    """The recording record view every module below writes through."""
    return _RecordingRecords()


@pytest.fixture
def store(monkeypatch, records: _RecordingRecords) -> _Store:
    """Install the recording store at the live ``pipeline.server`` binding.

    Every module below resolves ``_store`` through the server binding, so
    patching that one binding reaches all of them.
    """
    facade = _Store(records)
    monkeypatch.setattr(p, "_store", facade, raising=False)
    return facade


def _module_source(module_name: str) -> str:
    """Return the source of one module of the pipeline package."""
    return (Path(story_status.__file__).parent / module_name).read_text()


# -- checkpoint: the journal and the manifest -------------------------------


def test_checkpoint_journal_is_written_to_the_bound_partition(
    store, records
) -> None:
    """The checkpoint's journal entry lands in the bound partition."""
    with bind_namespace(NAMESPACE):
        checkpoint._append_journal(PLAN, STORY, {"event": "interrupted"})

    assert records.journal_writes == [(NAMESPACE, PLAN, STORY)]


def test_checkpoint_journal_is_written_to_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the checkpoint still journals to the default one."""
    checkpoint._append_journal(PLAN, STORY, {"event": "interrupted"})

    assert records.journal_writes == [("default", PLAN, STORY)]


# -- no_progress: the parked/redispatched manifest ------------------------


def _capacity_case(tmp_path, monkeypatch, store):
    """A story whose last log line reports a usage cap."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[boot] model=x\nClaude usage limit reached. Reset at 3pm.\n"
    )
    story = {"summary": "s", "status": "tests_passed", "rework_attempts": 0}
    manifest = {"epics": {}, "stories": {STORY: story}}
    monkeypatch.setattr(no_progress, "_notify_user", lambda *a, **k: None)
    return story, manifest, str(worktree)


def test_no_progress_manifest_is_written_to_the_bound_partition(
    tmp_path, monkeypatch, store, records
) -> None:
    """A usage-cap redispatch persists the manifest in the bound partition."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch, store)

    with bind_namespace(NAMESPACE):
        result = no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert records.manifest_writes == [(NAMESPACE, PLAN)]
    assert ("default", PLAN) not in records.manifests


def test_no_progress_manifest_is_written_to_the_default_partition_when_unbound(
    tmp_path, monkeypatch, store, records
) -> None:
    """With nothing bound the redispatch still persists to the default one."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch, store)

    result = no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert records.manifest_writes == [("default", PLAN)]


# -- triage: the sweep's manifest ------------------------------------------


def test_triage_sweep_manifest_is_written_to_the_bound_partition(
    monkeypatch, store, records
) -> None:
    """The triage sweep persists the manifest in the bound partition."""
    story = {"summary": "s", "status": "queued"}
    manifest = {"epics": {}, "stories": {STORY: story}}
    monkeypatch.setattr(triage, "_auto_triage_enabled", lambda: True)
    monkeypatch.setattr(triage, "triage_candidates", lambda stories: [])
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    records.manifests[(NAMESPACE, PLAN)] = manifest

    with bind_namespace(NAMESPACE):
        triage.run_triage_sweep(PLAN)

    assert ("default", PLAN) not in records.manifests


# -- the regression guard ---------------------------------------------------


@pytest.mark.parametrize("module_name", STORY_PATH_MODULES)
def test_story_path_module_never_names_the_default_partition(module_name: str) -> None:
    """No story-path module may name the default partition in a record call.

    A literal ``"default"`` in a record-store call is the defect this story
    removes; it is the regression guard against it creeping back.
    """
    source = _module_source(module_name)

    assert not _DEFAULT_PARTITION_CALL.search(source), (
        f"pipeline/{module_name} names the default partition in a record-store "
        "call; it must select the namespace bound for the call instead"
    )


@pytest.mark.parametrize("module_name", STORY_PATH_MODULES)
def test_story_path_module_uses_the_bound_records_wrappers(module_name: str) -> None:
    """Each story-path module reaches for the shared bound-namespace wrappers."""
    source = _module_source(module_name)

    assert "bound_records" in source, (
        f"pipeline/{module_name} must select the namespace through the "
        "pipeline.bound_records wrappers"
    )
    assert _BOUND_RECORDS_IMPORT.search(source), (
        f"pipeline/{module_name} must import the bound_records wrappers"
    )


def test_the_other_namespace_is_never_written_to(
    tmp_path, monkeypatch, store, records
) -> None:
    """A write under one binding never lands in another namespace."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch, store)

    with bind_namespace(NAMESPACE):
        no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert (OTHER_NAMESPACE, PLAN) not in records.manifests
    assert records.manifest_writes == [(NAMESPACE, PLAN)]
