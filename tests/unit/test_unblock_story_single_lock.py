"""``unblock_story`` takes the plan lock exactly once.

The tool opens the plan's transaction itself and then delegates the manifest
edit to :func:`pipeline.unpark.unpark_story`. On the configured Postgres store
``transaction()`` delegates to the plugin, so every entry opens its own session:
if ``unpark_story`` re-acquired the lock, the second session's
``pg_try_advisory_xact_lock`` would fail because the first still holds it, and
every call would fail with ``unblock failed: RuntimeError``.

The fake store below records every ``transaction()`` entry and raises if one is
entered while another is open, which is what a per-session lock does -- but
deterministically, and without a database. The FileStore tests keep passing
either way, because its flock is re-entrant within a thread.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager

import pytest

import pipeline.server as p
from app.identity import Principal
from pipeline import persistence as ppers
from pipeline import plan_approval
from pipeline import server_tools_lifecycle as lifecycle
from pipeline import unpark as unpark_mod
from pipeline.actor import bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.store import FileStore

PLAN = "forge"
STORY = "S1"
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
BASE_BRIEF = "Do the thing."


class _AuditLog:
    """Collects the audit records the authorization seam appends."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record, *args, **kwargs) -> None:
        self.records.append(record)


class _NestedLockStore:
    """A store whose plan lock is per-session: nesting is refused."""

    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        self.entries: list[str] = []
        self.open = 0
        self.nested = 0
        self.audit = _AuditLog()

    @contextmanager
    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        if self.open:
            self.nested += 1
            raise AssertionError(
                f"plan lock for {plan_name!r} taken again while already held"
            )
        self.entries.append(plan_name)
        self.open += 1
        try:
            yield True
        finally:
            self.open -= 1

    def get_manifest(self, plan_name: str) -> dict:
        assert plan_name == PLAN
        return copy.deepcopy(self.manifest)

    def save_manifest(self, plan_name: str, manifest: dict) -> None:
        assert plan_name == PLAN
        self.manifest = copy.deepcopy(manifest)

    def audit_log(self):
        """The authorization seam appends here; this test does not read it."""
        return self.audit


@pytest.fixture
def store(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    manifest = {
        "plan": PLAN,
        "owner": "https://idp#alice",
        "stories": {
            STORY: {
                "summary": "s",
                "status": "parked",
                "agent_instructions": BASE_BRIEF,
                "files": ["a.py"],
                "rework_attempts": 3,
            }
        },
    }
    # ``authorize`` reads the plan owner from the plan directory, so the file
    # form has to exist too; the store under test is the fake one below.
    FileStore().save_manifest(PLAN, manifest)
    fake = _NestedLockStore(manifest)
    monkeypatch.setattr(p, "_store", fake)
    return fake


@pytest.fixture
def notices(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        unpark_mod,
        "_notify_user",
        lambda plan, message, **kw: seen.append({"plan": plan, "message": message, **kw}),
    )
    monkeypatch.setattr(unpark_mod, "_record_resolution", lambda *a, **k: None)
    return seen


def _story(store: _NestedLockStore) -> dict:
    return copy.deepcopy(store.manifest["stories"][STORY])


def test_unblock_takes_the_plan_lock_once_and_succeeds(store, notices):
    with bind_principal(ALICE):
        result = lifecycle.unblock_story(
            plan_name=PLAN,
            story_key=STORY,
            clear_fields=["rework_attempts"],
            status="interrupted",
            add_files=["b.py"],
            note="fixed the fixture",
        )

    assert result["ok"] is True, result
    assert store.entries == [PLAN]
    assert store.nested == 0
    story = _story(store)
    assert "rework_attempts" not in story
    assert story["status"] == "interrupted"
    assert story["files"] == ["a.py", "b.py"]
    assert "fixed the fixture" in story["agent_instructions"]
    assert [n["event"] for n in notices] == ["story_unparked_manually"]


def test_the_default_path_still_takes_one_lock_on_the_configured_store(
    store, notices, monkeypatch
):
    """``unpark_story`` without ``store`` keeps building and locking once.

    This is the CLI's path: it must still take the plan lock itself (exactly
    once) and must not be handed the caller's session.
    """
    monkeypatch.setattr(unpark_mod, "build_store", lambda: store)

    result = unpark_mod.unpark_story(PLAN, STORY, ["rework_attempts"])

    assert store.entries == [PLAN]
    assert store.nested == 0
    assert result["cleared"] == ["rework_attempts"]
    assert _story(store)["status"] == "parked"
