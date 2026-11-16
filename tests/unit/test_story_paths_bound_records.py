"""The story-path modules read and write their records in the bound namespace.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so a manifest or journal that a story path reads or writes below a tick has to
land in the namespace bound for that tick.  Naming the default partition
instead sends the write to a partition the plan's own namespace never sees:
the scheduler reads the plan back from the bound partition and finds nothing.

These tests grade that through the public entry points of the seven modules
this story names - ``story_status``, ``checkpoint``, ``no_progress``,
``triage``, ``triage_scope_actions``, ``plan_conflict_ruling`` and
``escalation`` - and end with the regression guard: none of them may name the
default partition in the record-store calls this story moves, and each one
must reach for the shared ``bound_records`` wrappers.

With nothing bound the wrappers select the default partition, so the
background paths and the single-namespace installs keep writing exactly where
they wrote before; that is pinned here too, for both a read and a write.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline import (
    checkpoint,
    escalation,
    no_progress,
    story_status,
    triage,
)
from pipeline import server as p
from pipeline.actor import bind_namespace

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "S1"

#: The seven modules the story names, each with the record-store methods whose
#: calls this story moves off the default partition.
STORY_PATH_MODULES = {
    "story_status.py": ("get_manifest", "put_manifest"),
    "checkpoint.py": ("append_journal", "put_manifest"),
    "no_progress.py": ("put_manifest",),
    "triage.py": ("get_manifest",),
    "triage_scope_actions.py": ("put_manifest",),
    "plan_conflict_ruling.py": ("put_manifest",),
    "escalation.py": ("put_manifest",),
}

#: A record-store call that names the default partition literally.
_DEFAULT_PARTITION_CALL = re.compile(r'records\(\)\s*\.\s*(\w+)\(\s*"default"')

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
    """The recording record view every module below reads and writes through."""
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


def test_checkpoint_journal_is_written_to_the_bound_partition(store, records) -> None:
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


def _capacity_case(tmp_path, monkeypatch):
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
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch)

    with bind_namespace(NAMESPACE):
        result = no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert records.manifest_writes == [(NAMESPACE, PLAN)]
    assert ("default", PLAN) not in records.manifests


def test_no_progress_manifest_is_written_to_the_default_partition_when_unbound(
    tmp_path, monkeypatch, store, records
) -> None:
    """With nothing bound the redispatch still persists to the default one."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch)

    result = no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert records.manifest_writes == [("default", PLAN)]


def test_no_progress_never_writes_into_another_namespace(
    tmp_path, monkeypatch, store, records
) -> None:
    """A write under one binding never lands in a different namespace."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch)

    with bind_namespace(NAMESPACE):
        no_progress.classify_no_progress(PLAN, STORY, story, worktree, manifest)

    assert (OTHER_NAMESPACE, PLAN) not in records.manifests
    assert records.manifest_writes == [(NAMESPACE, PLAN)]


# -- triage: the sweep reads the plan's own partition ----------------------


def _triage_sweep(monkeypatch, records, namespace):
    """Run one triage sweep with the plan held in *namespace*."""
    manifest = {"epics": {}, "stories": {STORY: {"summary": "s", "status": "queued"}}}
    monkeypatch.setattr(triage, "_auto_triage_enabled", lambda: True)
    monkeypatch.setattr(triage, "triage_candidates", lambda stories: [])
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    records.manifests[(namespace, PLAN)] = manifest
    return manifest


def test_triage_sweep_reads_the_manifest_from_the_bound_partition(
    monkeypatch, store, records
) -> None:
    """The triage sweep reads the plan from the bound partition."""
    _triage_sweep(monkeypatch, records, NAMESPACE)

    with bind_namespace(NAMESPACE):
        triage.run_triage_sweep(PLAN)

    assert (NAMESPACE, PLAN) in records.manifest_reads
    assert ("default", PLAN) not in records.manifest_reads


def test_triage_sweep_reads_the_default_partition_when_unbound(
    monkeypatch, store, records
) -> None:
    """With nothing bound the sweep still reads the default partition."""
    _triage_sweep(monkeypatch, records, "default")

    triage.run_triage_sweep(PLAN)

    assert ("default", PLAN) in records.manifest_reads


def test_triage_sweep_never_reads_a_foreign_namespace(monkeypatch, store, records) -> None:
    """The sweep under one binding reads that namespace and no other."""
    _triage_sweep(monkeypatch, records, NAMESPACE)

    with bind_namespace(NAMESPACE):
        triage.run_triage_sweep(PLAN)

    assert ("default", PLAN) not in records.manifest_reads
    assert ("default", PLAN) not in records.manifests


# -- escalation: the reset manifest -----------------------------------------


def _escalation_manifest() -> dict:
    """A failed local story, the shape escalation is handed."""
    story = {
        "summary": "thing",
        "status": "failed",
        "pid": 4242,
        "worktree": "",
        "backend": "local",
        "model": "gemma4:26b-a4b-it-qat",
        "dispatch_attempts": 1,
        "dispatch_error": "boom",
    }
    return {"epics": {}, "stories": {STORY: story}, "repo_root": "/tmp"}


@pytest.fixture
def fake_git(monkeypatch):
    """Stub the git boundary so no real git is ever shelled out."""

    class _FakeSubprocess:
        def run(self, cmd, **kwargs):
            return None

    monkeypatch.setattr(escalation, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(escalation, "_notify_user", lambda *a, **k: None)


def _escalate_to_claude(manifest):
    escalation._escalate_to_claude(manifest, PLAN, STORY)


def _escalate_to_fallback(manifest):
    escalation._escalate_to_local_fallback_model(manifest, PLAN, STORY, "fallback-model")


ESCALATIONS = (_escalate_to_claude, _escalate_to_fallback)


@pytest.mark.parametrize("escalate", ESCALATIONS)
def test_escalation_manifest_is_written_to_the_bound_partition(
    fake_git, store, records, escalate
) -> None:
    """Both escalation paths persist the reset manifest in the bound partition."""
    manifest = _escalation_manifest()

    with bind_namespace(NAMESPACE):
        escalate(manifest)

    assert records.manifest_writes == [(NAMESPACE, PLAN)]
    assert ("default", PLAN) not in records.manifests


@pytest.mark.parametrize("escalate", ESCALATIONS)
def test_escalation_manifest_is_written_to_the_default_partition_when_unbound(
    fake_git, store, records, escalate
) -> None:
    """With nothing bound the escalation reset still lands in the default one."""
    manifest = _escalation_manifest()

    escalate(manifest)

    assert records.manifest_writes == [("default", PLAN)]


# -- the regression guard ---------------------------------------------------


@pytest.mark.parametrize("module_name", sorted(STORY_PATH_MODULES))
def test_story_path_module_never_names_the_default_partition(module_name: str) -> None:
    """No story-path module may name the default partition in a record call.

    A literal ``"default"`` in a record-store call is the defect this story
    removes; it is the regression guard against it creeping back.
    """
    source = _module_source(module_name)
    methods = STORY_PATH_MODULES[module_name]
    offenders = [
        match
        for match in _DEFAULT_PARTITION_CALL.findall(source)
        if match in methods
    ]

    assert not offenders, (
        f"pipeline/{module_name} names the default partition in a record-store "
        f"call ({sorted(set(offenders))}); it must select the namespace bound "
        "for the call instead"
    )


@pytest.mark.parametrize("module_name", sorted(STORY_PATH_MODULES))
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


@pytest.mark.parametrize("module_name", sorted(STORY_PATH_MODULES))
def test_story_path_module_migrates_every_site_of_its_record_calls(
    module_name: str,
) -> None:
    """Every record-store call of a migrated method goes through the wrappers.

    The wrapper call is counted per method, so a site left behind on the old
    form (or a call that lost its wrapper) shows up as a missing one.
    """
    source = _module_source(module_name)

    for method in STORY_PATH_MODULES[module_name]:
        on_the_old_form = len(
            re.findall(rf'records\(\)\s*\.\s*{method}\(\s*"default"', source)
        )
        through_the_wrapper = len(
            re.findall(rf"bound_records\s*\.\s*{method}\\(", source)
        )

        assert on_the_old_form == 0, (
            f"pipeline/{module_name} still names the default partition in a "
            f"{method} call"
        )
        assert through_the_wrapper >= 1, (
            f"pipeline/{module_name} must reach for bound_records.{method}()"
        )


def test_story_status_keeps_its_manifest_writes_through_the_store() -> None:
    """``story_status`` persists all twelve of its manifest writes via the store."""
    source = _module_source("story_status.py")

    assert len(re.findall(r"bound_records\s*\.\s*put_manifest\(", source)) == 12
    assert len(re.findall(r"bound_records\s*\.\s*get_manifest\(", source)) == 1


def test_story_status_keeps_its_undefined_name_suppressions() -> None:
    """The rewritten ``story_status`` calls keep their ``# noqa: F821`` marker.

    ``_store`` is injected into the module's globals at runtime, so every
    mention of it is an undefined name for the linter and needs the
    suppression the old call carried.
    """
    source = _module_source("story_status.py")
    wrappers = [
        line
        for line in source.splitlines()
        if re.search(r"bound_records\s*\.\s*(?:get_manifest|put_manifest)\(", line)
    ]

    assert wrappers, "story_status must reach for the bound_records wrappers"
    assert all("# noqa: F821" in line for line in wrappers), (
        "every bound_records call in story_status must keep its "
        "'# noqa: F821' suppression: _store is injected at runtime"
    )
