"""dispatch_story resumes with the journal held by the record store.

The resume journal used to be read through the ``_read_journal`` ``_ServerRef``
(``persistence._read_journal``) on the path that builds a journal filename. It
is now read through the record store, exactly as every other dispatch read is:
``_store.records().get_journal("default", plan_name, story_key)``.

These tests observe the journal at the boundary that consumes it - the
``resume_journal`` keyword handed to ``_build_dispatch_command`` (the prompt
composer), the same spy point ``tests/unit/test_dispatch_context.py`` uses.
Dispatch passes ``journal or None``, so "no journal" reaches the composer as an
empty value; the negative cases therefore accept ``None`` or ``[]``.
"""

import json
import logging

import pytest

from app import backend
from pipeline import dispatch as pdispatch
from pipeline import paths
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.store_records import InvalidRecordKeyError

PLAN = "p"
STORY = "S1"
# ``_validate_key`` accepts this key, the record store's ``validate_story_key``
# rejects it - the exact gap that must degrade to "no resume journal".
REJECTED_STORY = "_S1"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    for mod in (p, ppers, paths):
        monkeypatch.setattr(mod, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wt)
    return wt


@pytest.fixture
def builds(worktree_root, monkeypatch):
    """Wire the model/git seams and spy on the prompt-compose boundary.

    Returns the list of keyword dicts ``_build_dispatch_command`` was called
    with; an existing ``WORKTREE_ROOT/<story_key>`` directory makes the call a
    resume, so no worktree or git work is needed.
    """
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda cmd, **kw: type("P", (), {"pid": 1})(),
    )
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: None)
    real_build = p._build_dispatch_command
    calls = []

    def spy(*a, **kw):
        calls.append(dict(kw))
        return real_build(*a, **kw)

    monkeypatch.setattr(p, "_build_dispatch_command", spy)
    return calls


def _write_manifest(plan_dir, keys):
    stories = {
        key: {
            "summary": "x",
            "agent_instructions": "BRIEF-TEXT",
            "status": "todo",
            "dependencies": [],
        }
        for key in keys
    }
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


class _FakeRecords:
    """Record view that answers ``get_journal`` and delegates the rest."""

    def __init__(self, inner, journal=None, raises=None):
        self._inner = inner
        self.journal = journal
        self.raises = raises
        self.calls = []

    def get_journal(self, namespace, plan_name, story_key):
        self.calls.append((namespace, plan_name, story_key))
        if self.raises is not None:
            raise self.raises
        return self.journal

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FakeStore:
    """Store proxy: only ``records()`` is faked, everything else is real."""

    def __init__(self, inner, records):
        self._inner = inner
        self._records = records

    def records(self):
        return self._records

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_should_pass_the_store_journal_as_resume_journal_when_resuming(
    plan_dir, worktree_root, builds, monkeypatch
):
    # A realistic journal entry: the prompt composer indexes e["step"] and
    # e["summary"] directly, so a shape-only fixture would let the dispatch
    # fail after the spy recorded the kwarg and the assertion below would pass
    # vacuously.
    sentinel = [{"step": "s1", "summary": "wrote the parser", "next_hint": ""}]
    records = _FakeRecords(p._store.records(), journal=sentinel)
    monkeypatch.setattr(p, "_store", _FakeStore(p._store, records))
    _write_manifest(plan_dir, [STORY])
    (worktree_root / STORY).mkdir()

    result = p.dispatch_story(PLAN, STORY)

    # The journal must reach the composer in a form it can actually compose:
    # a KeyError there is swallowed by the service layer's blanket except and
    # would otherwise read as a passing spy assertion.
    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert records.calls == [("default", PLAN, STORY)]
    assert builds[0]["resume_journal"] is sentinel


def test_should_pass_no_journal_when_not_resuming(
    plan_dir, builds, monkeypatch
):
    records = _FakeRecords(
        p._store.records(),
        raises=AssertionError("a fresh dispatch must not read a journal"),
    )
    monkeypatch.setattr(p, "_store", _FakeStore(p._store, records))
    monkeypatch.setattr(pdispatch, "_create_fresh_worktree", lambda *a, **k: None)
    monkeypatch.setattr(
        pdispatch, "_refuse_if_remote_branch_taken", lambda *a, **k: None
    )
    _write_manifest(plan_dir, [STORY])

    result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert records.calls == []
    assert builds[0]["resume_journal"] in (None, [])


def test_should_resume_with_no_journal_when_the_journal_file_is_malformed(
    plan_dir, worktree_root, builds
):
    journal_path = plan_dir / f"{PLAN}.{STORY}.journal.json"
    _write_manifest(plan_dir, [STORY])
    (worktree_root / STORY).mkdir()
    journal_path.write_text("{not json")

    # The store's journal read fails open: no JSONDecodeError escapes dispatch.
    result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert builds[0]["resume_journal"] in (None, [])

    # The fail-open read left no bad state behind: a valid journal is read on
    # the very next resume of the same story, in a shape the composer can use.
    good = [{"step": "s1", "summary": "wrote the parser", "next_hint": ""}]
    journal_path.write_text(json.dumps(good))
    result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert builds[-1]["resume_journal"] == good


def test_should_resume_with_no_journal_for_a_story_key_the_store_rejects(
    plan_dir, worktree_root, builds, monkeypatch, caplog
):
    records = _FakeRecords(
        p._store.records(), raises=InvalidRecordKeyError("invalid story key")
    )
    monkeypatch.setattr(p, "_store", _FakeStore(p._store, records))
    _write_manifest(plan_dir, [STORY, REJECTED_STORY])
    for key in (STORY, REJECTED_STORY):
        (worktree_root / key).mkdir()

    with caplog.at_level(logging.WARNING):
        result = p.dispatch_story(PLAN, REJECTED_STORY)

    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert records.calls == [("default", PLAN, REJECTED_STORY)]
    assert builds[0]["resume_journal"] in (None, [])
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert any(PLAN in msg and REJECTED_STORY in msg for msg in messages), messages

    # A rejected key must not suppress a later, valid journal.
    records.raises = None
    records.journal = [{"step": "s2", "summary": "pushed the branch", "next_hint": ""}]
    result = p.dispatch_story(PLAN, STORY)

    assert result.get("ok") is not False, result
    assert "error" not in result, result
    assert builds[-1]["resume_journal"] == records.journal
