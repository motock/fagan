"""Checkpoint journal entries are written through the record store.

``pipeline/checkpoint.py`` used to call ``persistence._append_journal``
directly, which appends to ``<plan>.<story>.journal.json`` with no record
store in the path. Under ``PIPELINE_STORE=sqlite`` that meant a checkpoint's
journal entry never reached the database row every reader now consults, so a
resumed dispatch could not see the step it was resuming from.

Both writers (``_checkpoint_impl`` - the ``checkpoint`` tool - and
``_terminate_and_checkpoint`` - the manual interrupt and the dispatch
watchdog) now go through
``_store.records().append_journal("default", plan_name, story_key, record)``
at the same point in the same flow. The module-level name ``_append_journal``
is kept on purpose: it is the seam the watchdog evidence test spies on.

The tests pin three things beyond "it writes":

* the write is the *store's* - a fake store alone observes it and the legacy
  journal file is never created, so the migration cannot silently revert;
* a story key the store rejects (``_validate_key`` accepts ``"_S1"`` and
  ``"a..b"``; ``validate_story_key`` does not) fails closed for the tool: a
  generic ``{"ok": False, "error": "invalid story key"}`` that never echoes
  the key back; and
* the watchdog path still terminates the process and marks the story
  interrupted when that write is rejected - a journal write must never be
  able to keep a hung dispatched process alive.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

import pipeline.server as p
from pipeline import checkpoint
from pipeline.store_records import (
    InvalidRecordKeyError,
    validate_namespace,
    validate_plan_name,
    validate_story_key,
)

PLAN = "journal-plan"
STORY = "story-a"
# Valid for checkpoint's own parsers._validate_key, invalid for the store's
# validate_story_key. Production has no such keys (grepped), so tightening the
# error is a behaviour change with no live caller.
REJECTED_KEYS = ["_S1", "a..b"]


class _FakeStore:
    """A record store whose journal lives in memory and validates its keys.

    Only the surfaces both writers touch are implemented: the single-manifest
    read they go through ``_plan_manifest`` and ``records().append_journal``.
    ``append_journal`` validates exactly as the real stores do, so a key that
    ``_validate_key`` tolerates is rejected here for the same reason it would
    be rejected by memory/sqlite/file.
    """

    def __init__(self, plan_dir, manifests):
        self.plan_dir = plan_dir
        self.data = copy.deepcopy(manifests)
        self.journals: list[tuple[str, str, str, dict]] = []
        self.reads: list[tuple[str, str]] = []

    def records(self):
        return self

    def manifest_path(self, plan_name):
        return self.plan_dir / f"{plan_name}.manifest.json"

    def get_manifest(self, namespace, plan_name=None):
        if plan_name is None:  # pre-migration store.get_manifest(plan) form
            namespace, plan_name = "default", namespace
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self.reads.append((namespace, plan_name))
        stored = self.data.get(plan_name)
        return copy.deepcopy(stored) if stored is not None else None

    def get_manifests(self, namespace):
        assert namespace == "default"
        return copy.deepcopy(self.data)

    def put_manifest(self, namespace, plan_name, manifest):
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self.data[plan_name] = copy.deepcopy(manifest)

    def append_journal(self, namespace, plan_name, story_key, record):
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)  # raises InvalidRecordKeyError
        if not isinstance(record, dict):
            raise TypeError("record must be a dict")
        self.journals.append(
            (namespace, plan_name, story_key, copy.deepcopy(record))
        )


@pytest.fixture
def plan_dir(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    return d


def _manifest(*story_keys: str) -> dict:
    keys = story_keys or (STORY,)
    return {
        "epics": {},
        "stories": {
            key: {"summary": "thing", "status": "in_progress", "worktree": "/tmp/wt"}
            for key in keys
        },
    }


def _install(monkeypatch, plan_dir, manifest, commit_sha="deadbeef"):
    """Install a fake store and stub the checkpoint's two side effects."""
    store = _FakeStore(plan_dir, {PLAN: manifest})
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir, raising=False)
    monkeypatch.setattr(p, "_store", store, raising=False)
    committed: list[tuple] = []

    def _fake_commit_wip(worktree, story_key, step, **kwargs):
        committed.append((worktree, story_key, step))
        return commit_sha

    monkeypatch.setattr(checkpoint, "_commit_wip", _fake_commit_wip)
    return store, committed


def _journal_files(plan_dir):
    return sorted(path.name for path in plan_dir.glob("*.journal.json"))


# ---------- the tool path: _checkpoint_impl ----------


def test_should_append_the_checkpoint_record_through_the_record_store(
    plan_dir, monkeypatch
):
    """The checkpoint's journal entry is the store's record, not a local file."""
    store, committed = _install(monkeypatch, plan_dir, _manifest())

    result = checkpoint._checkpoint_impl(
        PLAN, STORY, "step-1", "did the thing", "carry on"
    )

    assert result["ok"] is True
    assert committed == [("/tmp/wt", STORY, "step-1")]
    assert len(store.journals) == 1
    namespace, plan_name, story_key, record = store.journals[0]
    assert (namespace, plan_name, story_key) == ("default", PLAN, STORY)
    assert record["step"] == "step-1"
    assert record["summary"] == "did the thing"
    assert record["next_hint"] == "carry on"
    assert record["commit"] == "deadbeef"
    assert record["ts"]
    # The result is the same record the store received - no second copy.
    assert result["step"] == record["step"]
    assert result["commit"] == record["commit"]
    # Nothing was appended to the legacy on-disk journal.
    assert _journal_files(plan_dir) == []


# ---------- the watchdog path: _terminate_and_checkpoint ----------


def test_should_append_the_watchdog_record_through_the_record_store(
    plan_dir, monkeypatch
):
    """The dispatch-watchdog entry (evidence included) is the store's record."""
    manifest = _manifest()
    store, committed = _install(monkeypatch, plan_dir, manifest)
    manifest_path = plan_dir / f"{PLAN}.manifest.json"

    # A reused pid would skip the kill entirely; take the real path but never
    # signal a live process from a unit test.
    monkeypatch.setattr(checkpoint, "_pid_started_after_dispatch", lambda *a: False)
    killed: list[tuple] = []
    monkeypatch.setattr(checkpoint.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(
        checkpoint, "_watchdog_evidence", lambda story, key: {"last_log_line": "tail"}
    )
    monkeypatch.setattr(checkpoint, "_snapshot_agent_log", lambda *a: None)

    sha = checkpoint._terminate_and_checkpoint(
        manifest,
        manifest_path,
        PLAN,
        STORY,
        manifest["stories"][STORY],
        pid=4242,
        step="dispatch_watchdog_timeout",
        summary="watchdog fired",
    )

    assert killed == [4242]
    assert sha == "deadbeef"
    assert committed == [("/tmp/wt", STORY, "dispatch_watchdog_timeout")]
    assert len(store.journals) == 1
    namespace, plan_name, story_key, record = store.journals[0]
    assert (namespace, plan_name, story_key) == ("default", PLAN, STORY)
    assert record["step"] == "dispatch_watchdog_timeout"
    assert record["summary"] == "watchdog fired"
    assert record["next_hint"] == ""
    assert record["commit"] == "deadbeef"
    assert record["ts"]
    assert record["last_log_line"] == "tail"
    assert _journal_files(plan_dir) == []


# ---------- the negative case: a story key the store rejects ----------


@pytest.mark.parametrize("story_key", REJECTED_KEYS)
def test_should_refuse_a_checkpoint_for_a_story_key_the_store_rejects(
    plan_dir, monkeypatch, story_key
):
    """A key only ``_validate_key`` accepts fails closed, and stays out of the
    error message (the rejected value may be a path fragment)."""
    store, _committed = _install(monkeypatch, plan_dir, _manifest(story_key))

    # The premise: the store really does reject the key this test feeds in.
    with pytest.raises(InvalidRecordKeyError):
        store.append_journal("default", PLAN, story_key, {"step": "probe"})

    result = checkpoint._checkpoint_impl(PLAN, story_key, "step-1", "did it")

    assert result == {"ok": False, "error": "invalid story key"}
    assert story_key not in json.dumps(result)
    assert store.journals == []
    assert _journal_files(plan_dir) == []


def test_should_still_terminate_when_the_watchdog_journal_write_is_rejected(
    plan_dir, monkeypatch, caplog
):
    """A rejected journal write must not keep a hung agent alive.

    The watchdog still SIGTERMs the pid, still re-saves the manifest with the
    story ``interrupted`` (dispatch-eligible for resume), and reports the
    problem as a WARNING - the kill is the point of the function.
    """
    story_key = REJECTED_KEYS[0]
    manifest = _manifest(story_key)
    store, _committed = _install(monkeypatch, plan_dir, manifest)
    manifest_path = plan_dir / f"{PLAN}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(checkpoint, "_pid_started_after_dispatch", lambda *a: False)
    killed: list[tuple] = []
    monkeypatch.setattr(checkpoint.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(checkpoint, "_watchdog_evidence", lambda story, key: {})
    monkeypatch.setattr(checkpoint, "_snapshot_agent_log", lambda *a: None)

    with caplog.at_level(logging.WARNING, logger="pipeline.checkpoint"):
        sha = checkpoint._terminate_and_checkpoint(
            manifest,
            manifest_path,
            PLAN,
            story_key,
            manifest["stories"][story_key],
            pid=4242,
            step="dispatch_watchdog_timeout",
            summary="watchdog fired",
        )

    assert killed == [4242], "the process must still be terminated"
    assert sha == "deadbeef"
    assert manifest["stories"][story_key]["status"] == "interrupted"
    assert manifest["stories"][story_key]["last_commit"] == "deadbeef"
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk["stories"][story_key]["status"] == "interrupted"
    assert store.journals == []
    assert _journal_files(plan_dir) == []

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name == "pipeline.checkpoint"
    ]
    assert len(warnings) == 1, [record.getMessage() for record in caplog.records]
    assert "journal" in warnings[0].getMessage().lower()
    # The rejected key is never echoed into the log either.
    assert story_key not in warnings[0].getMessage()
