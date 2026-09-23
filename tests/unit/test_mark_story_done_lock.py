"""Lock-guard tests for ``pipeline.ci._mark_story_done_impl`` (MCPHYG-2).

``_mark_story_done_impl`` is a read-modify-write on the plan manifest (it flips
a story to ``done`` and may emit the plan-completed notification). Without the
per-plan lock it can race the scheduler's 60s tick or a concurrent
dispatch/ingest/interrupt. The fix wraps the whole body in
``with _store.transaction(plan_name) as acquired:`` and returns
``{"ok": True, "skipped": "locked", "reason": ...}`` when the lock is held.

``_mark_story_done_impl`` is rebound (``types.FunctionType``) to
``pipeline.server``'s namespace at import time, so its bare-name reads of
``_store`` / ``get_ticket_provider`` resolve against the live
``pipeline.server`` module -- which is exactly what these tests patch.

These tests fail until the lock is added.
"""

import json
from contextlib import contextmanager

import pytest

import pipeline.server as p
from pipeline import ci as pci

LOCKED = {
    "ok": True,
    "skipped": "locked",
    "reason": "another dispatch/ingest/interrupt is in progress for this plan",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect pipeline.server.PLAN_DIR (and the persistence/concurrency
    copies) to an isolated tmp dir."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def _null_provider(monkeypatch):
    """A ticket provider whose set_state is a no-op (no Plane/network)."""

    class _NullSetStateProvider:
        def set_state(self, *args, **kwargs):
            return None

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"stories": stories})
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _manifest_bytes(plan_dir, plan_name):
    return (plan_dir / f"{plan_name}.manifest.json").read_bytes()


def _fake_transaction(monkeypatch, acquired):
    """Replace ``_store.transaction`` with a fake CM yielding ``acquired``."""

    @contextmanager
    def _txn(name):
        yield acquired

    monkeypatch.setattr(p._store, "transaction", _txn)


# ---------------------------------------------------------------------------
# Locked path: the lock is held by a concurrent operation.
# ---------------------------------------------------------------------------


def test_locked_returns_skipped_and_leaves_manifest_untouched(
    plan_dir, _null_provider, monkeypatch
):
    """When the plan lock is held, return the skipped/locked shape and do NOT
    mutate the manifest (story stays "in_progress")."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    before = _manifest_bytes(plan_dir, "p1")
    _fake_transaction(monkeypatch, acquired=False)

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == LOCKED
    assert _manifest_bytes(plan_dir, "p1") == before, (
        "manifest must be byte-for-byte unchanged when the lock is not acquired"
    )
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "in_progress"


def test_locked_does_not_call_set_state(plan_dir, monkeypatch):
    """The ticket provider must not be touched when the lock is not acquired."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    _fake_transaction(monkeypatch, acquired=False)

    calls = []

    class _RecordingProvider:
        def set_state(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _RecordingProvider())

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == LOCKED
    assert calls == [], f"set_state must not be called when locked; got {calls!r}"


def test_locked_does_not_call_save_manifest(plan_dir, _null_provider, monkeypatch):
    """The skipped path must not write the manifest at all."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    _fake_transaction(monkeypatch, acquired=False)

    saves = []
    monkeypatch.setattr(
        p._store, "save_manifest", lambda *a, **k: saves.append((a, k))
    )

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == LOCKED
    assert saves == [], f"save_manifest must not be called when locked; got {saves!r}"


def test_lock_released_then_second_call_succeeds(plan_dir, _null_provider, monkeypatch):
    """State across calls: a skipped call leaves the story untouched, so the
    follow-up call after the lock is released transitions it exactly once."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}, "S2": {"status": "todo"}})

    _fake_transaction(monkeypatch, acquired=False)
    first = pci._mark_story_done_impl("p1", "S1")
    assert first == LOCKED
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "in_progress"

    _fake_transaction(monkeypatch, acquired=True)
    second = pci._mark_story_done_impl("p1", "S1")
    assert second == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "done"


# ---------------------------------------------------------------------------
# Happy path: the lock IS acquired.
# ---------------------------------------------------------------------------


def test_happy_path_acquired_mutates_manifest(plan_dir, _null_provider, monkeypatch):
    """With the lock acquired the function still succeeds and flips the status
    (S2 stays open, so the plan is not reported complete)."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}, "S2": {"status": "todo"}})
    _fake_transaction(monkeypatch, acquired=True)

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "done"


def test_happy_path_uses_real_transaction(plan_dir, _null_provider):
    """Unpatched: the real flock-based transaction is acquired and the write
    lands (proves the wrapper does not break the normal path)."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}, "S2": {"status": "todo"}})

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "done"


def test_all_done_still_reports_plan_completed(plan_dir, _null_provider, monkeypatch):
    """The all-done branch (plan_completed payload) is preserved."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    _fake_transaction(monkeypatch, acquired=True)

    result = pci._mark_story_done_impl("p1", "S1")

    assert result == {"ok": True, "plan_completed": True, "stories": ["S1"]}


# ---------------------------------------------------------------------------
# Negative / boundary: nonexistent story_key.
# ---------------------------------------------------------------------------


def test_nonexistent_story_key_errors_when_lock_acquired(
    plan_dir, _null_provider, monkeypatch
):
    """A missing story still errors exactly as before (KeyError) when the lock
    is held -- the lock guard must not swallow or reshape that error."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    _fake_transaction(monkeypatch, acquired=True)

    with pytest.raises(KeyError):
        pci._mark_story_done_impl("p1", "NOPE")


def test_nonexistent_story_key_locked_returns_skipped(
    plan_dir, _null_provider, monkeypatch
):
    """The lock check precedes the story lookup: a locked plan short-circuits
    to the skipped shape regardless of the story_key."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "in_progress"}})
    _fake_transaction(monkeypatch, acquired=False)

    result = pci._mark_story_done_impl("p1", "NOPE")

    assert result == LOCKED
