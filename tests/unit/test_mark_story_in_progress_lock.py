"""Lock-guard tests for ``PipelineService.mark_story_in_progress`` (MCPHYG-2).

``mark_story_in_progress`` is a read-modify-write on the plan manifest. Without
the per-plan lock it can race the scheduler's 60s tick (or a concurrent
dispatch/ingest/interrupt) and clobber the other writer's manifest. The fix
wraps the whole body in ``with _store.transaction(plan_name) as acquired:``,
mirroring ``set_story_status``, and returns
``{"ok": True, "skipped": "locked", "reason": ...}`` when the lock is held.

The fake-lock mechanism mirrors
``tests/unit/test_config_write_overrides.py::test_set_plan_role_config_skipped_when_locked``:
replace ``pipeline.server._store.transaction`` with a context manager that
yields ``False``. ``pipeline.service`` resolves ``_store`` through a
``_ServerRef`` at call time, so patching the live ``pipeline.server`` store is
what the method actually sees.

These tests fail until the lock is added.
"""

import json
from contextlib import contextmanager

import pytest

import pipeline.server as p
from pipeline.service import PipelineService

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
    copies) to an isolated tmp dir, mirroring the per-file ``plan_dir``
    fixtures used across the rest of the unit suite."""
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
    mutate the manifest (story stays "todo")."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    before = _manifest_bytes(plan_dir, "p1")
    _fake_transaction(monkeypatch, acquired=False)

    result = PipelineService().mark_story_in_progress("p1", "S1")

    assert result == LOCKED
    assert _manifest_bytes(plan_dir, "p1") == before, (
        "manifest must be byte-for-byte unchanged when the lock is not acquired"
    )
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "todo"


def test_locked_does_not_call_save_manifest(plan_dir, _null_provider, monkeypatch):
    """The skipped path must not write the manifest at all."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    _fake_transaction(monkeypatch, acquired=False)

    saves = []
    monkeypatch.setattr(
        p._store, "save_manifest", lambda *a, **k: saves.append((a, k))
    )

    result = PipelineService().mark_story_in_progress("p1", "S1")

    assert result == LOCKED
    assert saves == [], f"save_manifest must not be called when locked; got {saves!r}"


def test_locked_does_not_call_set_state(plan_dir, monkeypatch):
    """The ticket provider must not be touched when the lock is not acquired."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    _fake_transaction(monkeypatch, acquired=False)

    calls = []

    class _RecordingProvider:
        def set_state(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _RecordingProvider())

    result = PipelineService().mark_story_in_progress("p1", "S1")

    assert result == LOCKED
    assert calls == [], f"set_state must not be called when locked; got {calls!r}"


def test_lock_released_then_second_call_succeeds(plan_dir, _null_provider, monkeypatch):
    """State across calls: a skipped call leaves the story untouched, so the
    follow-up call after the lock is released transitions it exactly once."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})

    _fake_transaction(monkeypatch, acquired=False)
    first = PipelineService().mark_story_in_progress("p1", "S1")
    assert first == LOCKED
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "todo"

    _fake_transaction(monkeypatch, acquired=True)
    second = PipelineService().mark_story_in_progress("p1", "S1")
    assert second == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Happy path: the lock IS acquired.
# ---------------------------------------------------------------------------


def test_happy_path_acquired_mutates_manifest(plan_dir, _null_provider, monkeypatch):
    """With the lock acquired the method still succeeds and flips the status."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    _fake_transaction(monkeypatch, acquired=True)

    result = PipelineService().mark_story_in_progress("p1", "S1")

    assert result == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "in_progress"


def test_happy_path_uses_real_transaction(plan_dir, _null_provider):
    """Unpatched: the real flock-based transaction is acquired and the write
    lands (proves the wrapper does not break the normal path)."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})

    result = PipelineService().mark_story_in_progress("p1", "S1")

    assert result == {"ok": True}
    assert _read_manifest(plan_dir, "p1")["stories"]["S1"]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Negative / boundary: nonexistent story_key.
# ---------------------------------------------------------------------------


def test_nonexistent_story_key_errors_when_lock_acquired(
    plan_dir, _null_provider, monkeypatch
):
    """A missing story still errors exactly as before when the lock is held."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    before = _manifest_bytes(plan_dir, "p1")
    _fake_transaction(monkeypatch, acquired=True)

    result = PipelineService().mark_story_in_progress("p1", "NOPE")

    assert result == {"ok": False, "error": "No such story NOPE"}
    assert _manifest_bytes(plan_dir, "p1") == before


def test_nonexistent_story_key_locked_returns_skipped(
    plan_dir, _null_provider, monkeypatch
):
    """The lock check precedes the story lookup: a locked plan short-circuits
    to the skipped shape regardless of the story_key."""
    _write_manifest(plan_dir, "p1", {"S1": {"status": "todo"}})
    _fake_transaction(monkeypatch, acquired=False)

    result = PipelineService().mark_story_in_progress("p1", "NOPE")

    assert result == LOCKED
