"""``unblock_story`` takes the plan lock exactly once.

The tool opens the plan's transaction itself and performs the manifest edit
through its module-local ``unpark_story`` helper, which reads and writes via
``_store``; it never calls :func:`pipeline.unpark.unpark_story`, which builds
its own store. On the configured Postgres store ``transaction()`` delegates to
the plugin, so every entry opens its own session: if the helper re-acquired the
lock from a second store instance, ``pg_try_advisory_xact_lock`` would return
false while the outer session still held it, and every call would fail with
``unblock failed: RuntimeError``.

The fakes below model that database rather than a thread: the lock state is
shared by every store instance over the same "database", and a second entry
yields ``False`` instead of blocking. The FileStore suite cannot see the bug
because its flock is re-entrant within a thread, which is why this file exists.
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


class _Database:
    """The state every session over this "database" shares."""

    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        self.entries: list[str] = []
        self.open = 0
        self.nested = 0


class _SessionStore:
    """One store instance: an independent session over the shared database."""

    def __init__(self, db: _Database) -> None:
        self.db = db
        self.audit = _AuditLog()

    @contextmanager
    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        if self.db.open:
            # The plan lock belongs to the session that already holds it, so a
            # second attempt returns false -- what the Postgres store does.
            self.db.nested += 1
            yield False
            return
        self.db.entries.append(plan_name)
        self.db.open += 1
        try:
            yield True
        finally:
            self.db.open -= 1

    def get_manifest(self, plan_name: str) -> dict:
        assert plan_name == PLAN
        return copy.deepcopy(self.db.manifest)

    def save_manifest(self, plan_name: str, manifest: dict) -> None:
        assert plan_name == PLAN
        self.db.manifest = copy.deepcopy(manifest)

    def audit_log(self):
        return self.audit


@pytest.fixture
def db(tmp_path, monkeypatch):
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
    # form has to exist too; the stores under test are the fakes below.
    FileStore().save_manifest(PLAN, manifest)
    database = _Database(manifest)
    monkeypatch.setattr(p, "_store", _SessionStore(database))
    # Every store this code builds is another session over the same database.
    monkeypatch.setattr(unpark_mod, "build_store", lambda: _SessionStore(database))
    return database


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


def _story(db: _Database) -> dict:
    return copy.deepcopy(db.manifest["stories"][STORY])


def test_unblock_takes_the_plan_lock_once_and_succeeds(db, notices):
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
    assert db.entries == [PLAN]
    assert db.nested == 0
    story = _story(db)
    assert "rework_attempts" not in story
    assert story["status"] == "interrupted"
    assert story["files"] == ["a.py", "b.py"]
    assert "fixed the fixture" in story["agent_instructions"]
    assert [n["event"] for n in notices] == ["story_unparked_manually"]


def test_the_default_path_still_takes_its_own_lock_once(db, notices):
    """The CLI path (``pipeline.unpark.unpark_story``) builds its own store and locks once.

    It must still acquire the plan lock itself, and exactly once.
    """
    result = unpark_mod.unpark_story(PLAN, STORY, ["rework_attempts"])

    assert db.entries == [PLAN]
    assert db.nested == 0
    assert result["cleared"] == ["rework_attempts"]
    assert _story(db)["status"] == "parked"
