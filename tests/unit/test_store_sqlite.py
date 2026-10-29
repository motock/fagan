"""``SqliteStore``: the single-host database ``Store`` (story ST-03).

Two properties are pinned here at construction time:

* the path must be absolute -- a relative path would resolve against whatever
  directory the MCP server, the dashboard or the unpark tool happened to start
  in, so two processes that "share" a store would silently write two different
  databases. The refusal names the setting and never echoes the rejected path,
  which may identify an operator's home directory.
* the database runs in WAL mode, so a reader (the dashboard) never blocks the
  writer (the MCP server).
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import store as store_module
from pipeline.store_sqlite import SqliteStore

PLAN = "s1"


@pytest.fixture()
def db_path(tmp_path):
    """The absolute database path every store in one test shares."""
    return str(tmp_path / "store.db")


@pytest.fixture()
def store(db_path, monkeypatch):
    """A ``SqliteStore`` on a fresh database, with the plans dir isolated.

    ``PLAN_DIR`` is pinned because the store keeps each manifest's on-disk
    projection (see ``manifest_path``) there; without it a test would write
    into the operator's real plans directory.
    """
    plan_dir = db_path + ".plans"
    Path(plan_dir).mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", Path(plan_dir))
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", Path(plan_dir))
    created = SqliteStore(db_path)
    yield created
    close = getattr(created, "close", None)
    if close is not None:
        close()


def test_relative_path_refused(tmp_path, monkeypatch):
    """A relative path cannot name one database for every process."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        SqliteStore("data/store.db")

    assert "data/store.db" not in str(excinfo.value)
    assert "store.db" not in str(excinfo.value), (
        "the refusal must name the setting, never the rejected path"
    )


def test_wal_mode_enabled(tmp_path):
    """The database is opened in WAL mode, visible from another connection."""
    SqliteStore(str(tmp_path / "s.db"))

    with sqlite3.connect(str(tmp_path / "s.db")) as probe:
        mode = probe.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"


def test_transaction_commits(store):
    """A transaction that exits normally leaves its write readable."""
    manifest = {"schema_version": 1, "stories": {"S1": {"status": "running"}}}

    with store.transaction(PLAN) as acquired:
        assert acquired is True, "an uncontended acquire must succeed"
        store.save_manifest(PLAN, manifest)

    assert store.get_manifest(PLAN) == manifest


def test_transaction_rolls_back_and_next_transaction_still_works(store):
    """A raised exception discards the write and leaves no open transaction."""
    with pytest.raises(RuntimeError), store.transaction(PLAN) as acquired:
        assert acquired is True
        store.save_manifest(PLAN, {"a": 1})
        raise RuntimeError("caller failed mid-transaction")

    assert store.get_manifest_or_none(PLAN) is None, "the write must be discarded"

    with store.transaction(PLAN) as acquired:
        assert acquired is True, (
            "the connection must not be left inside the rolled-back transaction"
        )
        store.save_manifest(PLAN, {"a": 2})

    assert store.get_manifest(PLAN) == {"a": 2}


def test_manifest_json_round_trip(store):
    """A nested manifest reads back as an equal document, not as text."""
    manifest = {"b": [1, 2], "a": {"nested": True}}

    store.save_manifest(PLAN, manifest)
    got = store.get_manifest(PLAN)

    assert got == {"b": [1, 2], "a": {"nested": True}}
    assert isinstance(got, dict)


INJECTION_KEY = "s1'); DROP TABLE story_manifests;--"


def test_injection_shaped_key_is_rejected_upstream():
    """Layer 1: the key boundary check refuses an injection-shaped key."""
    from pipeline.parsers import _validate_key

    with pytest.raises(ValueError):
        _validate_key(INJECTION_KEY)


def test_injection_shaped_key_stored_literally(store, db_path):
    """Layer 2: the store keeps such a key as data, not as SQL.

    The key never reaches the store in production (``_validate_key`` refuses it
    first), so this pins the second layer on its own: a parameterized statement
    stores the string verbatim and the schema survives.
    """
    manifest = {"schema_version": 1, "stories": {}}

    store.save_manifest(INJECTION_KEY, manifest)

    assert store.get_manifest(INJECTION_KEY) == manifest
    assert store.list_manifests() == [INJECTION_KEY]
    survived = sqlite3.connect(db_path).execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
        ("story_manifests",),
    ).fetchone()[0]
    assert survived == 1, "the table must not have been dropped by the key"

STORE_ENV = "PIPELINE_STORE"
PATH_ENV = "PIPELINE_STORE_PATH"


@pytest.fixture()
def store_env(monkeypatch):
    """Start every factory test from "neither variable is set"."""
    monkeypatch.delenv(STORE_ENV, raising=False)
    monkeypatch.delenv(PATH_ENV, raising=False)


@pytest.fixture()
def isolated_plan_dir(tmp_path, monkeypatch):
    """Keep the selected store's manifest projection out of the real plans dir."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", plan_dir)
    return plan_dir


def test_factory_builds_a_sqlite_store(store_env, isolated_plan_dir, tmp_path, monkeypatch):
    """``PIPELINE_STORE=sqlite`` plus an absolute path selects ``SqliteStore``."""
    db_path = tmp_path / "factory.db"
    monkeypatch.setenv(STORE_ENV, "sqlite")
    monkeypatch.setenv(PATH_ENV, str(db_path))

    built = store_module.build_store()

    assert type(built) is SqliteStore
    built.save_manifest(PLAN, {"a": 1})
    assert built.get_manifest(PLAN) == {"a": 1}
    assert db_path.exists(), "the store must open the configured database"


def test_factory_refuses_a_relative_sqlite_path(store_env, tmp_path, monkeypatch):
    """A relative path is refused, and the refusal does not echo it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(STORE_ENV, "sqlite")
    monkeypatch.setenv(PATH_ENV, "data/store.db")

    with pytest.raises(ValueError) as excinfo:
        store_module.build_store()

    assert "data/store.db" not in str(excinfo.value)


@pytest.mark.parametrize("selected", [None, "file"])
def test_factory_still_selects_the_file_store(
    store_env, isolated_plan_dir, monkeypatch, selected
):
    """Unset or ``file`` keeps the file store, whatever the path setting is.

    Only the type is asserted: the file store's internals are covered by its own
    tests.
    """
    if selected is not None:
        monkeypatch.setenv(STORE_ENV, selected)
    monkeypatch.setenv(PATH_ENV, str(isolated_plan_dir / "unused.db"))

    assert type(store_module.build_store()) is store_module.FileStore
# The loser of a contended acquire is refused after the store's bounded busy
# timeout (250ms) instead of waiting for the holder. The hold is longer than
# the deadline on purpose: a store relying on the sqlite3 default five-second
# timeout would block until the holder commits and miss this deadline.
_REFUSAL_DEADLINE_S = 0.9
_ROUNDS = 3
_HOLD_S = 1.2


def _contend(db_path, barrier, holders, refusals):
    """Hold the plan's write transaction in a loop, counting overlaps.

    Module level on purpose: ``spawn`` pickles the target by reference, so a
    closure or a locally defined function cannot be used. Each child opens its
    own store, because a connection cannot be shared across processes.
    """
    import pipeline.server  # noqa: F401
    from pipeline.store_sqlite import SqliteStore

    store = SqliteStore(db_path)
    try:
        barrier.wait(timeout=30)
        for _ in range(_ROUNDS):
            started = time.monotonic()
            with store.transaction(PLAN) as acquired:
                elapsed = time.monotonic() - started
                assert elapsed < _REFUSAL_DEADLINE_S, (
                    "a contended acquire blocked past the bounded busy timeout"
                )
                if acquired:
                    with holders.get_lock():
                        holders.value += 1
                        assert holders.value == 1, "two processes held the write lock"
                    time.sleep(_HOLD_S)
                    with holders.get_lock():
                        holders.value -= 1
                else:
                    with refusals.get_lock():
                        refusals.value += 1

        # A refusal must leave nothing open: the next attempt has to work.
        deadline = time.monotonic() + 30
        while True:
            with store.transaction(PLAN) as acquired:
                if acquired:
                    break
            assert time.monotonic() < deadline, "the write lock never became free"
    finally:
        store.close()


def test_two_processes_never_both_acquire(store, db_path):
    """Across processes, a contended acquire is refused, not waited on.

    Two processes attempt the plan's write transaction at the same time. A
    refused attempt must return within the bounded busy timeout rather than
    block until the holder finishes, the loser must never observe a second
    holder, and both stores must be usable afterwards.
    """
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    holders = ctx.Value("i", 0)
    refusals = ctx.Value("i", 0)

    processes = [
        ctx.Process(target=_contend, args=(db_path, barrier, holders, refusals))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)

    for process in processes:
        assert not process.is_alive(), "a contender hung"
        assert process.exitcode == 0, "a contender failed its bounded-acquire check"

    assert holders.value == 0, "a process ended still holding the write lock"
    assert refusals.value >= 1, "no contention was exercised; the test is vacuous"

    with store.transaction(PLAN) as acquired:
        assert acquired is True, "the write lock must be free after both contenders"
        store.save_manifest(PLAN, {"after": "contention"})
    assert store.get_manifest(PLAN) == {"after": "contention"}
