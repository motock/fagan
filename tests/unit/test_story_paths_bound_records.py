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

The same guard covers the six modules whose bulk manifest read, journal clear
and journal-key listing this story moves as well - ``advance``,
``concurrency``, ``cross_plan``, ``detached_grade``, ``escalation`` and
``wedge_io`` - and their behaviour is driven through their public entry
points with a namespace bound and with nothing bound.

With nothing bound the wrappers select the default partition, so the
background paths and the single-namespace installs keep reading and writing
exactly where they did before; that is pinned here too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from pipeline import (
    advance,
    bound_records,
    checkpoint,
    concurrency,
    cross_plan,
    detached_grade,
    escalation,
    no_progress,
    story_status,
    triage,
    wedge_io,
)
from pipeline import server as p
from pipeline.actor import bind_namespace

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "S1"

#: The seven modules the story names, each with the record-store methods whose
#: calls this story moves off the default partition, and the number of sites
#: of each method the story moves.
STORY_PATH_MODULES = {
    "story_status.py": {"get_manifest": 1, "put_manifest": 12},
    "checkpoint.py": {"append_journal": 1, "put_manifest": 1},
    "no_progress.py": {"put_manifest": 3},
    "triage.py": {"get_manifest": 2},
    "triage_scope_actions.py": {"put_manifest": 1},
    "plan_conflict_ruling.py": {"put_manifest": 1},
    "escalation.py": {"put_manifest": 2},
}

#: The modules whose bulk manifest read, journal clear and journal-key listing
#: this story moves off the default partition, each with the record-store
#: methods whose calls it moves and the number of sites of each method.
BOUND_RECORD_READ_MODULES = {
    "advance.py": {"get_manifests": 2},
    "concurrency.py": {"get_manifests": 1},
    "cross_plan.py": {"get_manifests": 1},
    "detached_grade.py": {"get_manifests": 1},
    "escalation.py": {"clear_journal": 2},
    "wedge_io.py": {"list_journal_keys": 1},
}

#: A record-store call that names the default partition literally.
_DEFAULT_PARTITION_CALL = re.compile(r'records\(\)\s*\.\s*(\w+)\(\s*"default"')

#: The import of the shared wrappers this story names.
_BOUND_RECORDS_IMPORT = re.compile(r"from\s+\.\s+import\s+[^\n]*\bbound_records\b")


class _RecordingRecords:
    """A record view that records the namespace of every call it forwards."""

    def __init__(self) -> None:
        self.manifest_writes: list[tuple[str, str]] = []
        self.manifest_reads: list[tuple[str, str]] = []
        self.manifest_bulk_reads: list[str] = []
        self.journal_writes: list[tuple[str, str, str]] = []
        self.journal_clears: list[tuple[str, str, str]] = []
        self.journal_key_reads: list[tuple[str, str]] = []
        self.manifests: dict[tuple[str, str], dict] = {}
        self.journals: dict[tuple[str, str, str], list[dict]] = {}

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        self.manifest_writes.append((namespace, plan_name))
        self.manifests[(namespace, plan_name)] = manifest

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        self.manifest_reads.append((namespace, plan_name))
        return self.manifests.get((namespace, plan_name))

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        self.manifest_bulk_reads.append(namespace)
        return {
            name: manifest
            for (held_namespace, name), manifest in self.manifests.items()
            if held_namespace == namespace
        }

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        self.journal_writes.append((namespace, plan_name, story_key))
        self.journals.setdefault((namespace, plan_name, story_key), []).append(record)

    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]:
        return list(self.journals.get((namespace, plan_name, story_key), []))

    def clear_journal(self, namespace: str, plan_name: str, story_key: str) -> None:
        self.journal_clears.append((namespace, plan_name, story_key))
        self.journals.pop((namespace, plan_name, story_key), None)

    def list_journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        self.journal_key_reads.append((namespace, plan_name))
        return sorted(
            key
            for (held_namespace, held_plan, key) in self.journals
            if held_namespace == namespace and held_plan == plan_name
        )


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


#: The check_story_status family: story_status.py and the phase modules it was
#: split into. Its record-store calls now live across all of them.
STORY_STATUS_FAMILY_FILES = (
    "story_status.py",
    "story_status_liveness.py",
    "story_status_failures.py",
    "story_status_tests.py",
    "story_status_review_reuse.py",
)


def _module_source(module_name: str) -> str:
    """Return the source of one module of the pipeline package.

    ``story_status.py`` names the whole check_story_status family, so its
    source is read across the phase modules the body was split into.
    """
    package = Path(story_status.__file__).parent
    names = STORY_STATUS_FAMILY_FILES if module_name == "story_status.py" else (module_name,)
    return "\n".join((package / name).read_text() for name in names)


# -- checkpoint: the journal entry ------------------------------------------


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
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(no_progress.usage_cooldown, "trip", lambda *a, **k: None)
    monkeypatch.setattr(no_progress.usage_transitions, "announce", lambda *a, **k: None)
    return story, manifest, str(worktree)


def test_no_progress_manifest_is_written_to_the_bound_partition(
    tmp_path, monkeypatch, store, records
) -> None:
    """A usage-cap redispatch persists the manifest in the bound partition."""
    story, manifest, worktree = _capacity_case(tmp_path, monkeypatch)

    with bind_namespace(NAMESPACE):
        result = no_progress.classify_no_progress(
            PLAN, STORY, story, worktree, manifest
        )

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
    """Hold one plan in *namespace* and stub the sweep's triage seams."""
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


def test_triage_sweep_never_reads_a_foreign_namespace(
    monkeypatch, store, records
) -> None:
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
    """Stub the git boundary and the diagnosis, so nothing is spawned."""

    class _FakeSubprocess:
        def run(self, cmd, **kwargs):
            return None

    monkeypatch.setattr(escalation, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(escalation, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "collect_attempt_facts", lambda *a, **k: "")
    monkeypatch.setattr(escalation, "collect_failure_evidence", lambda *a, **k: "")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: None)


def _escalate_to_claude(manifest):
    escalation._escalate_to_claude(manifest, PLAN, STORY)


def _escalate_to_fallback(manifest):
    escalation._escalate_to_local_fallback_model(
        manifest, PLAN, STORY, "fallback-model"
    )


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


# -- story_status: the plan is read from the bound partition ---------------


def test_story_status_reads_the_manifest_from_the_bound_partition(store, records) -> None:
    """``check_story_status`` reads the plan from the bound partition.

    The body of ``check_story_status`` resolves its bare names against the
    server namespace, so this also proves the wrapper is reachable from there.
    """
    with bind_namespace(NAMESPACE):
        result = p.check_story_status(PLAN, STORY)

    assert result == {"ok": False, "error": "Plan not found"}
    assert records.manifest_reads == [(NAMESPACE, PLAN)]
    assert ("default", PLAN) not in records.manifest_reads


def test_story_status_reads_the_default_partition_when_unbound(store, records) -> None:
    """With nothing bound the story status still reads the default partition."""
    result = p.check_story_status(PLAN, STORY)

    assert result == {"ok": False, "error": "Plan not found"}
    assert records.manifest_reads == [("default", PLAN)]


# -- the regression guard ---------------------------------------------------


def test_no_story_path_module_names_the_default_partition() -> None:
    """No story-path module may name the default partition in a record call.

    A literal ``"default"`` in a record-store call is the defect this story
    removes; it is the regression guard against it creeping back.
    """
    offenders = {}
    for module_name, methods in STORY_PATH_MODULES.items():
        hits = [
            match
            for match in _DEFAULT_PARTITION_CALL.findall(_module_source(module_name))
            if match in methods
        ]
        if hits:
            offenders[module_name] = sorted(set(hits))

    assert not offenders, (
        "these modules still name the default partition in a record-store call; "
        f"they must select the namespace bound for the call instead: {offenders}"
    )


def test_every_story_path_module_reaches_the_bound_namespace() -> None:
    """Each story-path module selects the namespace through the wrappers."""
    missing_wrappers = [
        module_name
        for module_name in STORY_PATH_MODULES
        if "bound_records" not in _module_source(module_name)
    ]
    missing_import = [
        module_name
        for module_name in STORY_PATH_MODULES
        if not _BOUND_RECORDS_IMPORT.search(_module_source(module_name))
    ]

    assert not missing_wrappers, f"no bound-namespace wrappers in: {missing_wrappers}"
    assert not missing_import, f"no 'from . import bound_records' in: {missing_import}"


def test_every_story_path_site_goes_through_the_wrapper() -> None:
    """Every record-store call this story moves goes through the wrapper.

    The wrapper call is counted per method, so a site left behind on the old
    form - or a call that lost its wrapper - shows up as a missing one.
    """
    shortfalls = {}
    for module_name, methods in STORY_PATH_MODULES.items():
        source = _module_source(module_name)
        for method, expected in methods.items():
            found = len(re.findall(rf"bound_records\s*\.\s*{method}\(", source))
            if found != expected:
                shortfalls[f"{module_name}:{method}"] = (found, expected)

    assert not shortfalls, (
        "these record-store sites do not go through the bound_records wrapper "
        f"(found, expected): {shortfalls}"
    )


def _calls_with_their_trailing_text(source: str) -> list[str]:
    """Return each bound-records call together with the text that follows it.

    The call is matched up to its closing parenthesis, so a call wrapped over
    several lines is returned whole, and the rest of that line is returned with
    it, so a suppression written after the call is part of the returned text.
    """
    calls = []
    for match in re.finditer(r"bound_records\s*\.\s*\w+\(", source):
        depth = 0
        end = match.end()
        for index in range(match.end() - 1, len(source)):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        line_end = source.find("\n", end)
        calls.append(source[match.start() : len(source) if line_end == -1 else line_end])
    return calls


def test_story_status_keeps_its_undefined_name_suppressions() -> None:
    """The rewritten ``story_status`` calls read ``_store`` through the server.

    ``_store`` is no longer a bare undefined name: each call reads it from
    ``server_bindings.server()``, so no ``# noqa: F821`` suppression remains.
    """
    source = _module_source("story_status.py")
    calls = _calls_with_their_trailing_text(source)

    assert calls, "story_status must reach for the bound_records wrappers"
    assert all("server_bindings.server()._store" in call for call in calls), (
        "every bound_records call in story_status must read _store through "
        "server_bindings.server()"
    )
    assert "noqa: F821" not in source


# -- the bound record reads: manifests, journals and journal keys --------------
#
# The bulk manifest read, the journal clear and the journal-key listing each
# used to name the default partition.  Under a bound namespace that reads (or
# clears) the wrong partition: the concurrency counter would size its slots
# against another namespace's agents, the wedge probe would inspect the wrong
# journal, and an escalation reset would clear the default team's journal
# while leaving the plan's own in place.


def test_get_manifests_returns_the_manifests_of_the_bound_partition(
    store, records
) -> None:
    """``bound_records.get_manifests`` reads the bound partition only."""
    records.manifests[(NAMESPACE, "plan-bound")] = {"where": "bound"}
    records.manifests[("default", "plan-default")] = {"where": "default"}

    with bind_namespace(NAMESPACE):
        manifests = bound_records.get_manifests(store)

    assert manifests == {"plan-bound": {"where": "bound"}}


def test_get_manifests_reads_the_default_partition_when_unbound(store, records) -> None:
    """With nothing bound the bulk read still reads the default partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {"where": "bound"}
    records.manifests[("default", "plan-default")] = {"where": "default"}

    manifests = bound_records.get_manifests(store)

    assert manifests == {"plan-default": {"where": "default"}}


def test_clear_journal_clears_the_bound_partition_only(store, records) -> None:
    """``bound_records.clear_journal`` forgets the bound partition's journal."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, STORY)] = [{"event": "other team"}]

    with bind_namespace(NAMESPACE):
        bound_records.clear_journal(store, PLAN, STORY)

    assert records.journals.get((NAMESPACE, PLAN, STORY)) is None
    assert records.journals[("default", PLAN, STORY)] == [{"event": "other team"}]


def test_clear_journal_clears_the_default_partition_when_unbound(store, records) -> None:
    """With nothing bound the clear still clears the default partition."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, STORY)] = [{"event": "other team"}]

    bound_records.clear_journal(store, PLAN, STORY)

    assert records.journals.get(("default", PLAN, STORY)) is None
    assert records.journals[(NAMESPACE, PLAN, STORY)] == [{"event": "boom"}]


def test_list_journal_keys_lists_the_bound_partition_only(store, records) -> None:
    """``bound_records.list_journal_keys`` lists the bound partition's keys."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, "other-story")] = [{"event": "x"}]

    with bind_namespace(NAMESPACE):
        keys = bound_records.list_journal_keys(store, PLAN)

    assert keys == [STORY]


def test_list_journal_keys_lists_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the key listing still lists the default partition."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, "other-story")] = [{"event": "x"}]

    assert bound_records.list_journal_keys(store, PLAN) == ["other-story"]


# -- cross_plan: the neighbour manifests --------------------------------------


def test_neighbour_manifests_are_read_from_the_bound_partition(store, records) -> None:
    """``neighbour_manifests`` reads the neighbours of the bound partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {"where": "bound"}
    records.manifests[("default", "plan-default")] = {"where": "default"}

    with bind_namespace(NAMESPACE):
        neighbours = cross_plan.neighbour_manifests(store, PLAN)

    assert neighbours == {"plan-bound": {"where": "bound"}}


def test_neighbour_manifests_read_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the neighbour read still reads the default one."""
    records.manifests[(NAMESPACE, "plan-bound")] = {"where": "bound"}
    records.manifests[("default", "plan-default")] = {"where": "default"}

    neighbours = cross_plan.neighbour_manifests(store, PLAN)

    assert neighbours == {"plan-default": {"where": "default"}}

# -- wedge_io: the journal keys the probe inspects ----------------------------


def test_wedge_journal_keys_are_read_from_the_bound_partition(store, records) -> None:
    """The wedge probe lists the journal keys of the bound partition."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, "other-story")] = [{"event": "x"}]

    with bind_namespace(NAMESPACE):
        keys = wedge_io._list_journal_keys(PLAN)

    assert keys == [STORY]


def test_wedge_journal_keys_read_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the wedge probe still lists the default partition."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, "other-story")] = [{"event": "x"}]

    assert wedge_io._list_journal_keys(PLAN) == ["other-story"]


# -- concurrency and advance: the agents that hold a slot ---------------------


def _live_story() -> dict:
    """A story that looks like a running dispatched agent."""
    return {"status": "in_progress", "pid": os.getpid()}


def _two_live_stories() -> dict:
    """Two stories that look like running dispatched agents."""
    return {"stories": {"s2": _live_story(), "s3": _live_story()}}


def test_in_progress_agents_are_counted_in_the_bound_partition_only(
    store, records
) -> None:
    """The concurrency counter sizes its slots from the bound partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _live_story()}
    }
    records.manifests[("default", "plan-default")] = _two_live_stories()

    with bind_namespace(NAMESPACE):
        count = concurrency._count_in_progress_agents()

    assert count == 1


def test_in_progress_agents_are_counted_in_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the counter still counts the default partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _live_story()}
    }
    records.manifests[("default", "plan-default")] = _two_live_stories()

    assert concurrency._count_in_progress_agents() == 2


def test_on_device_agents_are_counted_in_the_bound_partition_only(
    store, records
) -> None:
    """The on-device counter sizes its slots from the bound partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _live_story()}
    }
    records.manifests[("default", "plan-default")] = _two_live_stories()

    with bind_namespace(NAMESPACE):
        count = advance._count_on_device_in_progress_agents()

    assert count == 1


def test_on_device_agents_are_counted_in_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the on-device counter reads the default partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _live_story()}
    }
    records.manifests[("default", "plan-default")] = _two_live_stories()

    assert advance._count_on_device_in_progress_agents() == 2


# -- detached_grade: the grades that hold a slot ------------------------------


def _grading_story() -> dict:
    """A story that looks like it holds a live detached grade."""
    return {"grading_pid": os.getpid()}


def _two_grading_plans(records) -> None:
    """Two plans of the default partition, each holding a live detached grade."""
    records.manifests[("default", "plan-default-a")] = {
        "stories": {"s2": _grading_story()}
    }
    records.manifests[("default", "plan-default-b")] = {
        "stories": {"s3": _grading_story()}
    }


def test_live_grades_are_counted_in_the_bound_partition_only(store, records) -> None:
    """The grade counter reads the live grades of the bound partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _grading_story()}
    }
    _two_grading_plans(records)

    with bind_namespace(NAMESPACE):
        count = detached_grade.live_grade_count()

    assert count == 1


def test_live_grades_are_counted_in_the_default_partition_when_unbound(
    store, records
) -> None:
    """With nothing bound the grade counter reads the default partition."""
    records.manifests[(NAMESPACE, "plan-bound")] = {
        "stories": {STORY: _grading_story()}
    }
    _two_grading_plans(records)

    assert detached_grade.live_grade_count() == 2


# -- escalation: the journal the reset clears ---------------------------------


@pytest.mark.parametrize("escalate", ESCALATIONS)
def test_escalation_clears_the_journal_in_the_bound_partition_only(
    fake_git, store, records, escalate
) -> None:
    """The escalation reset clears the plan's own journal, not another team's."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, STORY)] = [{"event": "other team"}]

    with bind_namespace(NAMESPACE):
        escalate(_escalation_manifest())

    assert records.journal_clears == [(NAMESPACE, PLAN, STORY)]
    assert records.journals.get((NAMESPACE, PLAN, STORY)) is None
    assert records.journals[("default", PLAN, STORY)] == [{"event": "other team"}]


@pytest.mark.parametrize("escalate", ESCALATIONS)
def test_escalation_clears_the_default_partition_journal_when_unbound(
    fake_git, store, records, escalate
) -> None:
    """With nothing bound the reset still clears the default partition."""
    records.journals[(NAMESPACE, PLAN, STORY)] = [{"event": "boom"}]
    records.journals[("default", PLAN, STORY)] = [{"event": "other team"}]

    escalate(_escalation_manifest())

    assert records.journal_clears == [("default", PLAN, STORY)]
    assert records.journals.get(("default", PLAN, STORY)) is None
    assert records.journals[(NAMESPACE, PLAN, STORY)] == [{"event": "boom"}]


# -- the regression guard for the bound record reads --------------------------


def test_no_bound_record_read_module_names_the_default_partition() -> None:
    """None of the six modules names the default partition any more.

    A literal ``"default"`` in one of the three record-store calls this story
    moves is the defect it removes; this is the regression guard against it
    creeping back.
    """
    offenders = {}
    for module_name, methods in BOUND_RECORD_READ_MODULES.items():
        hits = [
            match
            for match in _DEFAULT_PARTITION_CALL.findall(_module_source(module_name))
            if match in methods
        ]
        if hits:
            offenders[module_name] = sorted(set(hits))

    assert not offenders, (
        "these modules still name the default partition in a record-store call; "
        f"they must select the namespace bound for the call instead: {offenders}"
    )


def test_every_bound_record_read_module_reaches_the_bound_namespace() -> None:
    """Each of the six modules selects the namespace through the wrappers."""
    missing_wrappers = [
        module_name
        for module_name in BOUND_RECORD_READ_MODULES
        if "bound_records" not in _module_source(module_name)
    ]
    missing_import = [
        module_name
        for module_name in BOUND_RECORD_READ_MODULES
        if not _BOUND_RECORDS_IMPORT.search(_module_source(module_name))
    ]

    assert not missing_wrappers, f"no bound-namespace wrappers in: {missing_wrappers}"
    assert not missing_import, f"no 'from . import bound_records' in: {missing_import}"


def test_every_bound_record_read_site_goes_through_the_wrapper() -> None:
    """Every record-store site this story moves goes through the wrapper.

    The wrapper call is counted per method, so a site left behind on the old
    form - or a call that lost its wrapper - shows up as a missing one.
    """
    shortfalls = {}
    for module_name, methods in BOUND_RECORD_READ_MODULES.items():
        source = _module_source(module_name)
        for method, expected in methods.items():
            found = len(re.findall(rf"bound_records\s*\.\s*{method}\(", source))
            if found < expected:
                shortfalls[f"{module_name}:{method}"] = (found, expected)

    assert not shortfalls, (
        "these record-store sites do not go through the bound_records wrapper "
        f"(found, expected): {shortfalls}"
    )
