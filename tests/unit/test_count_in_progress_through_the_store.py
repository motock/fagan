"""The in-progress agent counter reads every plan through the record store.

Story: "Migrate concurrency's agent count and zombie reaping to the record
store" (store-c2-r2).

``_count_in_progress_agents`` reads ``_store.records().get_manifests("default")``
rather than globbing ``PLAN_DIR``. The pid liveness rules and the live
test-author-phase rule are unchanged; these tests pin them through the store.
``get_manifests`` skips an unreadable manifest with a warning, so one bad file
must not raise out of the counter.

``os.kill`` is monkeypatched at the process boundary (``concurrency.os.kill``)
so liveness is deterministic without signalling a real process.
"""

from __future__ import annotations

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import concurrency as pcon
from pipeline import store as pstore


def _file_store() -> pstore.FileStore:
    fn = getattr(pstore, "build_store", None)
    assert fn is not None, "pipeline.store.build_store is not defined"
    return fn({"PIPELINE_STORE": "file"})


@pytest.fixture
def counter(tmp_path, monkeypatch):
    """A real FileStore rooted at ``tmp_path`` wired into ``pipeline.server``."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", plans)
    store = _file_store()
    monkeypatch.setattr(pipeline.server, "_store", store)
    return store, plans


def _write(store, plan_name: str, stories: dict) -> None:
    store.records().put_manifest("default", plan_name, {"stories": stories})


class _Kill:
    """A fake ``os.kill``: ``ProcessLookupError`` for dead pids, and
    ``PermissionError`` for pids that exist but are not ours to signal."""

    def __init__(self, dead=(), denied=()):
        self.dead = set(dead)
        self.denied = set(denied)

    def __call__(self, pid, sig):
        if pid in self.denied:
            raise PermissionError(1, "Operation not permitted")
        if pid in self.dead:
            raise ProcessLookupError(3, "No such process")
        # Every other pid is alive: returning normally means "alive".


def test_should_count_live_in_progress_stories_across_two_plans(counter, monkeypatch):
    store, _ = counter
    _write(store, "p1", {
        "a": {"summary": "a", "status": "in_progress", "pid": 11},
        "b": {"summary": "b", "status": "in_progress", "pid": 22},
        "c": {"summary": "c", "status": "todo"},
    })
    _write(store, "p2", {
        "d": {"summary": "d", "status": "in_progress", "pid": 33},
        "e": {"summary": "e", "status": "done"},
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill())

    assert pcon._count_in_progress_agents() == 3


def test_should_not_count_a_dead_pid_story(counter, monkeypatch):
    store, _ = counter
    _write(store, "p1", {
        "a": {"summary": "live", "status": "in_progress", "pid": 11},
        "b": {"summary": "dead", "status": "in_progress", "pid": 22},
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill(dead=[22]))

    assert pcon._count_in_progress_agents() == 1


def test_should_count_a_permission_denied_pid_as_running(counter, monkeypatch):
    """``PermissionError`` means the process exists but is not ours to signal:
    it still occupies a slot, so it must be counted."""
    store, _ = counter
    _write(store, "p1", {
        "a": {"summary": "someone else's", "status": "in_progress", "pid": 7000},
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill(denied=[7000]))

    assert pcon._count_in_progress_agents() == 1


def test_should_count_a_live_test_author_phase_without_in_progress_status(
    counter, monkeypatch
):
    """A detached test-author phase holds a local model loaded, so it occupies
    a slot even though its story status is not ``in_progress``."""
    store, _ = counter
    _write(store, "p1", {
        "a": {
            "summary": "authoring",
            "status": "todo",
            "test_author_phase": {"pid": 5151},
        },
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill())

    assert pcon._count_in_progress_agents() == 1


def test_should_not_count_a_dead_test_author_phase(counter, monkeypatch):
    store, _ = counter
    _write(store, "p1", {
        "a": {
            "summary": "authoring",
            "status": "todo",
            "test_author_phase": {"pid": 5151},
        },
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill(dead=[5151]))

    assert pcon._count_in_progress_agents() == 0


def test_should_skip_an_unreadable_manifest_instead_of_raising(counter, monkeypatch):
    """``get_manifests`` skips a malformed manifest with a warning, so the
    counter still counts the readable plans and does not raise."""
    store, plans = counter
    _write(store, "good", {
        "a": {"summary": "live", "status": "in_progress", "pid": 11},
    })
    (plans / "broken.manifest.json").write_text("{not valid json")
    monkeypatch.setattr(pcon.os, "kill", _Kill())

    assert pcon._count_in_progress_agents() == 1


def test_should_return_zero_when_there_are_no_plans(counter):
    assert pcon._count_in_progress_agents() == 0
