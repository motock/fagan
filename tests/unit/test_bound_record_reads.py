"""The bulk manifest read, the journal clear and the journal-key listing all
select the namespace bound for the call.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so every manifest, journal and decision read or write below a tick has to land
in the namespace bound for that tick.  Naming the default partition instead
sends the read to a partition the plan's own namespace never sees: the
concurrency counter would size its slots against another namespace's agents,
the wedge probe would inspect the wrong journal, and an escalation reset would
clear the default team's journal while leaving the plan's own in place.

``pipeline.bound_records`` owns that selection once.  These tests grade it
through the public entry points - the three wrappers on the helper module and
the six callers that reach them - and show that, with nothing bound, the
default partition is still the one that is read and cleared.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from pipeline import advance, concurrency, cross_plan, detached_grade, escalation
from pipeline import server as p
from pipeline import wedge_io
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

REPO_ROOT = Path(__file__).resolve().parents[2]

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
OTHER_PLAN = "plan-y"
STORY = "story-1"

#: The three record-store methods this story moves off the default partition.
MOVED_METHODS = ("get_manifests", "clear_journal", "list_journal_keys")

#: The modules that name the default partition in one of those calls today.
MODULES = (
    "advance.py",
    "concurrency.py",
    "cross_plan.py",
    "detached_grade.py",
    "escalation.py",
    "wedge_io.py",
)

#: ``records().get_manifests("default")`` and friends: a record-store call that
#: still names the default partition literally.
DEFAULT_PARTITION_CALL = re.compile(
    r"records\(\)\s*\.\s*(?:" + "|".join(MOVED_METHODS) + r")\s*\(\s*[\"']default[\"']"
)


def _source(module: str) -> str:
    return (REPO_ROOT / "pipeline" / module).read_text(encoding="utf-8")


class _Store:
    """A store facade whose record view is one in-memory record store.

    The in-memory record store selects the namespace per call, so a test sees
    the *partition selection* and nothing else.
    """

    def __init__(self) -> None:
        self.records_ = InMemoryRecordStore()

    def records(self) -> InMemoryRecordStore:
        return self.records_


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def server_store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """Install the store at the live ``pipeline.server`` binding.

    Every module below resolves ``_store`` through the server binding (the
    ``_ServerRef`` pattern, or a call-time ``from .server import _store``), so
    patching that one binding reaches all of them.
    """
    facade = _Store()
    monkeypatch.setattr(p, "_store", facade, raising=False)
    return facade


def _manifest(**stories: dict) -> dict:
    return {"epics": {}, "stories": dict(stories)}


# -- the three wrappers -------------------------------------------------------


def test_get_manifests_returns_every_manifest_in_the_bound_partition(
    store: _Store,
) -> None:
    """``get_manifests`` reads the manifests of the bound partition only."""
    store.records().put_manifest(NAMESPACE, PLAN, {"where": "bound"})
    store.records().put_manifest(DEFAULT_NAMESPACE, OTHER_PLAN, {"where": "default"})

    with bind_namespace(NAMESPACE):
        manifests = __import__("pipeline.bound_records", fromlist=["x"]).get_manifests(
            store
        )

    assert manifests == {PLAN: {"where": "bound"}}


def test_get_manifests_reads_the_default_partition_when_unbound(store: _Store) -> None:
    """With nothing bound the bulk read still reads the default partition."""
    store.records().put_manifest(NAMESPACE, PLAN, {"where": "bound"})
    store.records().put_manifest(DEFAULT_NAMESPACE, OTHER_PLAN, {"where": "default"})

    manifests = __import__("pipeline.bound_records", fromlist=["x"]).get_manifests(store)

    assert manifests == {OTHER_PLAN: {"where": "default"}}


def test_clear_journal_clears_the_bound_partition_only(store: _Store) -> None:
    """``clear_journal`` forgets the journal of the bound partition only."""
    store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    store.records().append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"step": "boom"})

    with bind_namespace(NAMESPACE):
        __import__("pipeline.bound_records", fromlist=["x"]).clear_journal(
            store, PLAN, STORY
        )

    assert store.records().get_journal(NAMESPACE, PLAN, STORY) == []
    assert store.records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        {"step": "boom"}
    ]


def test_clear_journal_clears_the_default_partition_when_unbound(store: _Store) -> None:
    """With nothing bound the clear still clears the default partition."""
    store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    store.records().append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {"step": "boom"})

    __import__("pipeline.bound_records", fromlist=["x"]).clear_journal(store, PLAN, STORY)

    assert store.records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []
    assert store.records().get_journal(NAMESPACE, PLAN, STORY) == [{"step": "boom"}]


def test_list_journal_keys_lists_the_bound_partition_only(store: _Store) -> None:
    """``list_journal_keys`` lists the keys of the bound partition only."""
    store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    store.records().append_journal(DEFAULT_NAMESPACE, PLAN, "other-story", {"x": 1})

    with bind_namespace(NAMESPACE):
        keys = __import__("pipeline.bound_records", fromlist=["x"]).list_journal_keys(
            store, PLAN
        )

    assert keys == [STORY]


def test_list_journal_keys_lists_the_default_partition_when_unbound(
    store: _Store,
) -> None:
    """With nothing bound the key listing still lists the default partition."""
    store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    store.records().append_journal(DEFAULT_NAMESPACE, PLAN, "other-story", {"x": 1})

    keys = __import__("pipeline.bound_records", fromlist=["x"]).list_journal_keys(
        store, PLAN
    )

    assert keys == ["other-story"]


# -- cross_plan: the neighbour manifests --------------------------------------


def test_neighbour_manifests_are_read_from_the_bound_partition(store: _Store) -> None:
    """``neighbour_manifests`` reads the neighbours of the bound partition."""
    store.records().put_manifest(NAMESPACE, OTHER_PLAN, {"where": "bound"})
    store.records().put_manifest(DEFAULT_NAMESPACE, "plan-default", {"where": "default"})

    with bind_namespace(NAMESPACE):
        neighbours = cross_plan.neighbour_manifests(store, PLAN)

    assert neighbours == {OTHER_PLAN: {"where": "bound"}}


def test_neighbour_manifests_read_the_default_partition_when_unbound(
    store: _Store,
) -> None:
    """With nothing bound the neighbour read still reads the default one."""
    store.records().put_manifest(NAMESPACE, OTHER_PLAN, {"where": "bound"})
    store.records().put_manifest(DEFAULT_NAMESPACE, "plan-default", {"where": "default"})

    neighbours = cross_plan.neighbour_manifests(store, PLAN)

    assert neighbours == {"plan-default": {"where": "default"}}


# -- wedge_io: the journal keys the probe inspects -----------------------------


def test_wedge_journal_keys_are_read_from_the_bound_partition(
    server_store: _Store,
) -> None:
    """The wedge probe lists the journal keys of the bound partition."""
    server_store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    server_store.records().append_journal(DEFAULT_NAMESPACE, PLAN, "other", {"x": 1})

    with bind_namespace(NAMESPACE):
        keys = wedge_io._list_journal_keys(PLAN)

    assert keys == [STORY]


def test_wedge_journal_keys_read_the_default_partition_when_unbound(
    server_store: _Store,
) -> None:
    """With nothing bound the wedge probe still lists the default partition."""
    server_store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    server_store.records().append_journal(DEFAULT_NAMESPACE, PLAN, "other", {"x": 1})

    assert wedge_io._list_journal_keys(PLAN) == ["other"]


# -- concurrency: the agents that hold a slot ---------------------------------


def test_in_progress_agents_are_counted_in_the_bound_partition_only(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concurrency counter sizes its slots from the bound partition."""
    monkeypatch.setattr(concurrency.os, "kill", lambda pid, sig: None)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"status": "in_progress", "pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"status": "in_progress", "pid": 222}})
    )

    with bind_namespace(NAMESPACE):
        count = concurrency._count_in_progress_agents()

    assert count == 1


def test_in_progress_agents_are_counted_in_the_default_partition_when_unbound(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing bound the counter still counts the default partition."""
    monkeypatch.setattr(concurrency.os, "kill", lambda pid, sig: None)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"status": "in_progress", "pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"status": "in_progress", "pid": 222}})
    )

    assert concurrency._count_in_progress_agents() == 1


# -- advance: the on-device agents that hold a slot ---------------------------


def test_on_device_agents_are_counted_in_the_bound_partition_only(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The on-device counter sizes its slots from the bound partition."""
    monkeypatch.setattr(advance.os, "kill", lambda pid, sig: None)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"status": "in_progress", "pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"status": "in_progress", "pid": 222}})
    )

    with bind_namespace(NAMESPACE):
        count = advance._count_on_device_in_progress_agents()

    assert count == 1


def test_on_device_agents_are_counted_in_the_default_partition_when_unbound(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing bound the on-device counter reads the default partition."""
    monkeypatch.setattr(advance.os, "kill", lambda pid, sig: None)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"status": "in_progress", "pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"status": "in_progress", "pid": 222}})
    )

    assert advance._count_on_device_in_progress_agents() == 1


# -- detached_grade: the grades that hold a slot ------------------------------


def test_live_grades_are_counted_in_the_bound_partition_only(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grade counter reads the live grades of the bound partition."""
    monkeypatch.setattr(detached_grade, "_is_pid_alive", lambda pid: True)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"grading_pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"grading_pid": 222}})
    )

    with bind_namespace(NAMESPACE):
        count = detached_grade.live_grade_count()

    assert count == 1


def test_live_grades_are_counted_in_the_default_partition_when_unbound(
    server_store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing bound the grade counter reads the default partition."""
    monkeypatch.setattr(detached_grade, "_is_pid_alive", lambda pid: True)
    server_store.records().put_manifest(
        NAMESPACE, PLAN, _manifest(**{STORY: {"grading_pid": 111}})
    )
    server_store.records().put_manifest(
        DEFAULT_NAMESPACE, "plan-default", _manifest(**{"s2": {"grading_pid": 222}})
    )

    assert detached_grade.live_grade_count() == 1


# -- escalation: the journal the reset clears ---------------------------------


def _failed_story() -> dict:
    return {
        "summary": "thing",
        "status": "failed",
        "pid": 4242,
        "worktree": "",
        "backend": "local",
        "model": "gemma4:26b-a4b-it-qat",
        "dispatch_attempts": 1,
        "dispatch_error": "boom",
    }


@pytest.fixture
def stub_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git boundary and the diagnosis, so nothing is spawned."""

    class _FakeSubprocess:
        def run(self, cmd, **kwargs):
            return None

    monkeypatch.setattr(escalation, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(escalation, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(escalation, "collect_attempt_facts", lambda *a, **k: "")
    monkeypatch.setattr(escalation, "collect_failure_evidence", lambda *a, **k: "")
    monkeypatch.setattr(escalation, "diagnose_failure", lambda *a, **k: None)


ESCALATIONS = ("_escalate_to_claude", "_escalate_to_local_fallback_model")


@pytest.mark.parametrize("name", ESCALATIONS)
def test_escalation_clears_the_journal_in_the_bound_partition_only(
    name: str, stub_escalation: None, server_store: _Store
) -> None:
    """The escalation reset clears the plan's own journal, not another one's."""
    server_store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    server_store.records().append_journal(
        DEFAULT_NAMESPACE, PLAN, STORY, {"step": "other team"}
    )
    manifest = _manifest(**{STORY: _failed_story()})
    manifest["repo_root"] = "/tmp"

    with bind_namespace(NAMESPACE):
        getattr(escalation, name)(manifest, PLAN, STORY)

    assert server_store.records().get_journal(NAMESPACE, PLAN, STORY) == []
    assert server_store.records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        {"step": "other team"}
    ]


@pytest.mark.parametrize("name", ESCALATIONS)
def test_escalation_clears_the_default_partition_journal_when_unbound(
    name: str, stub_escalation: None, server_store: _Store
) -> None:
    """With nothing bound the reset still clears the default partition."""
    server_store.records().append_journal(NAMESPACE, PLAN, STORY, {"step": "boom"})
    server_store.records().append_journal(
        DEFAULT_NAMESPACE, PLAN, STORY, {"step": "other team"}
    )
    manifest = _manifest(**{STORY: _failed_story()})
    manifest["repo_root"] = "/tmp"

    getattr(escalation, name)(manifest, PLAN, STORY)

    assert server_store.records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []
    assert server_store.records().get_journal(NAMESPACE, PLAN, STORY) == [
        {"step": "boom"}
    ]


# -- the regression guard ------------------------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_no_record_store_call_in_the_module_names_the_default_partition(
    module: str,
) -> None:
    """None of the six modules names the default partition any more."""
    offenders = DEFAULT_PARTITION_CALL.findall(_source(module))

    assert offenders == [], (
        f"pipeline/{module} still names the default partition at {offenders!r}; "
        "read and clear the bound namespace through pipeline.bound_records"
    )


@pytest.mark.parametrize("module", MODULES)
def test_the_module_uses_the_bound_record_wrappers(module: str) -> None:
    """Each of the six modules reaches the record store through the wrappers."""
    assert "bound_records" in _source(module), (
        f"pipeline/{module} must select the bound namespace through "
        "pipeline.bound_records"
    )


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_the_bound_record_wrappers(module: str) -> None:
    """Each of the six modules imports the helper module it now reaches for."""
    assert re.search(
        r"from\s+\.\s+import\s+[^\n]*\bbound_records\b", _source(module)
    ), f"pipeline/{module} must import pipeline.bound_records"
