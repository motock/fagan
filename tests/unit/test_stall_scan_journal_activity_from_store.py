"""Stall-scan activity time comes from the record store, not from PLAN_DIR.

``pipeline.stall_scan._newest_activity_mtime`` used to ``stat`` the journal
path itself (``PLAN_DIR / f"{plan_name}.{story_key}.journal.json"``). The
journal half now asks the store: ``records().get_journal_updated_at(
"default", plan_name, story_key)``. The ``agent.log`` half stays a worktree
artifact (read off the story's ``worktree`` path).

Graded here: the "newest wins" rule across the two signals, the ``None``
handling (a missing journal must never become 0.0 nor ``now()``), the equal
timestamp boundary pinning the legacy behaviour, and that the read is PURE --
the store's journal time is never written back nor cached in a module global.
"""

from __future__ import annotations

import os

import pytest

import pipeline.server as p
from pipeline import stall_scan

PLAN = "stall-store-journal-plan"
KEY = "stall-store-journal-story"

JOURNAL_TS = 1_700_000_000.0
AGENT_TS = 1_700_000_500.0
EQUAL_TS = 1_700_000_400.0


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class _StubRecords:
    """The namespaced record view; only the journal-activity read is used."""

    def __init__(self, store):
        self._store = store

    def get_journal_updated_at(self, namespace, plan_name, story_key):
        self._store.journal_calls.append((namespace, plan_name, story_key))
        return self._store.journal_updated_at


class _StubStore:
    """Minimal store exposing what the stall scan resolves.

    ``records`` is a METHOD on the real store (``FileStore.records()`` returns
    a ``FileRecords`` view), so the double mirrors that shape.
    """

    def __init__(self, journal_updated_at=None):
        self.journal_updated_at = journal_updated_at
        self.journal_calls = []
        self.records_calls = 0

    def get_manifest_or_none(self, plan_name):
        return None

    def records(self):
        self.records_calls += 1
        return _StubRecords(self)


def _install_store(monkeypatch, journal_updated_at=None):
    store = _StubStore(journal_updated_at)
    monkeypatch.setattr(p, "_store", store)
    return store


def _story_with_agent_log(tmp_path, mtime):
    """A story whose worktree holds an ``agent.log`` last written at ``mtime``."""
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    log = worktree / "agent.log"
    log.write_text("agent output\n")
    os.utime(log, (mtime, mtime))
    return {"status": "interrupted", "worktree": str(worktree)}


def _activity(plan_name=PLAN, story_key=KEY, story=None):
    return stall_scan._newest_activity_mtime(plan_name, story_key, story or {})


# --------------------------------------------------------------------------- #
# The journal half now comes from the store
# --------------------------------------------------------------------------- #


def test_should_use_the_store_journal_time_when_newer_than_agent_log(
    tmp_path, monkeypatch
):
    """Journal (store) newer than agent.log -> the journal time wins."""
    store = _install_store(monkeypatch, journal_updated_at=AGENT_TS)
    story = _story_with_agent_log(tmp_path, JOURNAL_TS)

    assert _activity(story=story) == AGENT_TS
    # The read goes through the records facade with the default namespace and
    # this story's own keys -- not a module-level PLAN_DIR path.
    assert store.journal_calls == [("default", PLAN, KEY)]


def test_should_use_agent_log_time_when_newer_than_the_journal(tmp_path, monkeypatch):
    """agent.log newer than the journal -> the worktree artifact wins."""
    _install_store(monkeypatch, journal_updated_at=JOURNAL_TS)
    story = _story_with_agent_log(tmp_path, AGENT_TS)

    assert _activity(story=story) == pytest.approx(AGENT_TS)


def test_should_use_agent_log_alone_when_the_store_has_no_journal(
    tmp_path, monkeypatch
):
    """No journal (store returns None) -> agent.log alone, never 0.0/now()."""
    _install_store(monkeypatch, journal_updated_at=None)
    story = _story_with_agent_log(tmp_path, AGENT_TS)

    assert _activity(story=story) == pytest.approx(AGENT_TS)


def test_should_report_no_activity_when_neither_exists(tmp_path, monkeypatch):
    """Neither signal exists -> None, so the caller fails open (no stall)."""
    _install_store(monkeypatch, journal_updated_at=None)
    worktree = tmp_path / "empty-wt"
    worktree.mkdir()

    assert _activity(story={"worktree": str(worktree)}) is None


def test_equal_timestamps_keep_the_legacy_result(tmp_path, monkeypatch):
    """Boundary: equal journal/agent times return the same float master did."""
    _install_store(monkeypatch, journal_updated_at=EQUAL_TS)
    story = _story_with_agent_log(tmp_path, EQUAL_TS)

    result = _activity(story=story)

    assert isinstance(result, float), f"expected epoch float, got {type(result)}"
    assert result == pytest.approx(EQUAL_TS)


# --------------------------------------------------------------------------- #
# The journal read is pure: no touch, no write-back, no cached maximum
# --------------------------------------------------------------------------- #


def test_journal_read_is_pure_and_the_answer_is_not_cached(tmp_path, monkeypatch):
    """A second call re-reads the store and ignores the earlier maximum."""
    store = _install_store(monkeypatch, journal_updated_at=JOURNAL_TS)
    first = _story_with_agent_log(tmp_path / "a", AGENT_TS)

    assert _activity(story=first) == pytest.approx(AGENT_TS)
    # A pure read: the store's journal time is untouched by the scan.
    assert store.journal_updated_at == JOURNAL_TS
    assert store.journal_calls == [("default", PLAN, KEY)]

    second = _story_with_agent_log(tmp_path / "b", JOURNAL_TS + 200.0)
    assert _activity(story=second) == pytest.approx(JOURNAL_TS + 200.0)
    assert store.journal_updated_at == JOURNAL_TS
    assert store.journal_calls == [
        ("default", PLAN, KEY),
        ("default", PLAN, KEY),
    ]
