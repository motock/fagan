"""Contract suite for the ``Store`` Protocol in ``pipeline/store.py``.

Every ``Store`` implementation -- ``FileStore`` (JSON files in the plans
directory) today, and any other backend registered later -- must satisfy every
assertion in this file unchanged. The suite is parametrized by the
``store_factory`` fixture, so registering a backend means extending that
fixture's ``params`` and the whole contract applies to it.

Behavior is read from ``FileStore`` as it is: a missing manifest raises
``FileNotFoundError`` instead of returning an empty dict, reads never mutate
state, a save replaces the whole document instead of merging, and
``transaction()`` is non-blocking -- a contended acquire yields ``False``
rather than waiting.
"""

import json
import threading

import pytest

import pipeline.persistence

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server
from pipeline.store import FileStore, Store
from pipeline.store_contract_notifications import NotificationHistoryContract
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = "contract-plan"


def _plans_dir():
    """The directory holding the plan files the file store reads and writes.

    The fixture patches ``pipeline.server.PLAN_DIR`` to the plans directory, so
    tests seed raw log files where the file backend will look for them.
    """
    return pipeline.server.PLAN_DIR


@pytest.fixture(params=["file", "sqlite"], ids=["file", "sqlite"])
def store_factory(tmp_path, monkeypatch, request):
    """A fresh Store per call, backed by an empty plans directory.

    Parametrized so additional Store implementations can be registered by
    extending ``params``; every entry must satisfy this contract.
    """
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", plan_dir)
    # pipeline.persistence holds its own module-level PLAN_DIR binding, and
    # FileStore's decision/journal appenders delegate to it. Pointing it at the
    # same directory keeps every store backend isolated from the operator's
    # real plans directory and keeps its writers and readers consistent.
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", plan_dir)
    created = []

    def _make():
        if request.param == "sqlite":
            store = SqliteStore(str(tmp_path / "contract.db"))
        else:
            store = FileStore()
        created.append(store)
        return store

    yield _make
    for store in created:
        if hasattr(store, "close"):
            store.close()


def test_get_manifest_missing(store_factory):
    """A plan that was never saved has no manifest to read."""
    store = store_factory()

    assert store.get_manifest_or_none(PLAN) is None

    with pytest.raises(FileNotFoundError):
        store.get_manifest(PLAN)


M1 = {"schema_version": 1, "stories": {"S1": {"status": "pending", "title": "T"}}}
EMPTY = {"schema_version": 1, "stories": {}}


def test_manifest_save_get_round_trip(store_factory):
    """A saved manifest reads back unchanged, and reading twice changes nothing."""
    store = store_factory()

    store.save_manifest(PLAN, M1)
    first = store.get_manifest(PLAN)
    second = store.get_manifest(PLAN)

    assert first == M1
    assert second == M1
    assert first == second, "a repeated read must return the same document"
    if isinstance(store, FileStore):
        assert (pipeline.server.PLAN_DIR / f"{PLAN}.manifest.json").exists()


def test_manifest_save_replaces_previous(store_factory):
    """Saving again replaces the whole document; it does not merge stories."""
    store = store_factory()
    store.save_manifest(PLAN, M1)

    store.save_manifest(PLAN, EMPTY)
    got = store.get_manifest(PLAN)

    assert got == EMPTY
    assert "S1" not in got["stories"], "a save replaces the document instead of merging"


def test_transaction_mutual_exclusion_across_threads(store_factory):
    """One holder at a time: a contended acquire refuses and holds nothing.

    Traces the lock across four actors: A holds and writes, B is refused while
    A holds (A's lock survives B's failed attempt), C waits for A to release the
    lock and then acquires, sees A's write, and releases it, then D acquires
    after C releases and sees C's write.
    """
    store = store_factory()
    store.save_manifest(
        PLAN, {"schema_version": 1, "stories": {"S1": {"status": "pending"}}}
    )

    a_acquired = threading.Event()
    a_released = threading.Event()
    b_checked = threading.Event()
    c_released = threading.Event()
    failures = []

    def _run(fn):
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced via failures
            failures.append(exc)

    def _set_status(status):
        manifest = store.get_manifest(PLAN)
        manifest["stories"]["S1"]["status"] = status
        store.save_manifest(PLAN, manifest)

    def holder_a():
        try:
            with store.transaction(PLAN) as acquired:
                assert acquired is True, "the first acquire must hold the plan lock"
                a_acquired.set()
                assert b_checked.wait(timeout=5), "the contender never finished"
                _set_status("running")
        finally:
            # Set only after the context manager exits: the lock is still held until
            # then, so signalling earlier would let C race against A. A finally keeps
            # that ordering while still waking C if A fails, so a broken A surfaces as
            # A's own error instead of C's misleading "the holder never released".
            a_released.set()

    def contender_b():
        try:
            assert a_acquired.wait(timeout=5), "the holder never acquired"
            with store.transaction(PLAN) as acquired:
                assert acquired is False, "a second holder must be refused"
        finally:
            b_checked.set()

    def holder_c():
        # Wait on A's release, not on B: B never held the lock, so b_checked
        # says nothing about whether A still holds it.
        assert a_released.wait(timeout=5), "the holder never released"
        try:
            with store.transaction(PLAN) as acquired:
                assert acquired is True, (
                    "the lock must be free after the holder releases"
                )
                assert store.get_manifest(PLAN)["stories"]["S1"]["status"] == "running"
                _set_status("done")
        finally:
            c_released.set()

    threads = [
        threading.Thread(target=_run, args=(holder_a,), name="a", daemon=True),
        threading.Thread(target=_run, args=(contender_b,), name="b", daemon=True),
        threading.Thread(target=_run, args=(holder_c,), name="c", daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "a transaction participant hung"

    assert not failures, (
        "a transaction participant failed: "
        + ", ".join(type(exc).__name__ for exc in failures)
    )
    assert c_released.is_set(), "the post-release holder never acquired"

    with store.transaction(PLAN) as acquired:
        assert acquired is True, "the lock must be free after every release"
        assert store.get_manifest(PLAN)["stories"]["S1"]["status"] == "done"


def _save_story(store, story):
    store.save_manifest(PLAN, {"schema_version": 1, "stories": {"S1": story}})


def test_update_story_preserves_other_fields(store_factory):
    """Each update patches the story saved by the previous update."""
    store = store_factory()
    _save_story(store, {"status": "pending", "title": "T", "notes": ""})

    updated = store.update_story(PLAN, "S1", {"status": "running"})
    after_first = store.get_manifest(PLAN)["stories"]["S1"]

    assert updated == {"status": "running", "title": "T", "notes": ""}
    assert after_first == {"status": "running", "title": "T", "notes": ""}

    updated = store.update_story(PLAN, "S1", {"notes": "n"})
    after_second = store.get_manifest(PLAN)["stories"]["S1"]

    assert updated == {"status": "running", "title": "T", "notes": "n"}
    assert after_second == {"status": "running", "title": "T", "notes": "n"}


def test_update_story_missing_story_behavior(store_factory):
    """A missing story is not created: the update reports none applied."""
    store = store_factory()
    _save_story(store, {"status": "pending", "title": "T"})

    assert store.update_story(PLAN, "S2", {"status": "running"}) is None
    assert "S2" not in store.get_manifest(PLAN)["stories"], (
        "a missing story must not be created by an update"
    )


def test_decisions_append_read_order(store_factory):
    """Decisions accumulate in append order, and reads do not consume them."""
    store = store_factory()

    for decision_id in (1, 2, 3):
        store.append_decision(PLAN, {"id": decision_id})
    assert [d["id"] for d in store.get_decisions(PLAN)] == [1, 2, 3]

    store.append_decision(PLAN, {"id": 4})
    assert [d["id"] for d in store.get_decisions(PLAN)] == [1, 2, 3, 4]

    store.append_decision(PLAN, {"id": 5})
    assert [d["id"] for d in store.get_decisions(PLAN)] == [1, 2, 3, 4, 5]


def test_decisions_read_has_no_side_effects(store_factory):
    """Reading decisions neither creates the log nor drops appended records."""
    store = store_factory()

    assert store.get_decisions(PLAN) == []
    assert store.get_decisions(PLAN) == [], "a read must not create an empty log"

    store.append_decision(PLAN, {"id": 1})
    first = store.get_decisions(PLAN)
    second = store.get_decisions(PLAN)

    assert first == second == [{"id": 1}]


STORY = "S1"


def test_journal_missing_returns_exact_empty_value(store_factory):
    """A story with no journal reports unavailable and no entries."""
    store = store_factory()

    assert store.get_journal(PLAN, STORY) == (False, [])
    assert store.get_journal_final_ts(PLAN, STORY) is None


def test_journal_read_and_append_order(store_factory):
    """Journal entries accumulate in append order and report the last timestamp."""
    store = store_factory()

    store.append_journal(PLAN, STORY, {"ts": "t1", "step": "one"})
    available, entries = store.get_journal(PLAN, STORY)

    assert available is True
    assert [entry["step"] for entry in entries] == ["one"]
    assert entries[0]["ts"] == "t1"
    assert entries[0]["next_hint"] is None
    assert store.get_journal_final_ts(PLAN, STORY) == "t1"

    store.append_journal(PLAN, STORY, {"ts": "t2", "step": "two", "summary": "x"})
    available, entries = store.get_journal(PLAN, STORY)

    assert available is True
    assert [entry["step"] for entry in entries] == ["one", "two"]
    assert entries[1]["summary"] == "x"
    assert available, "a read must not consume the journal"
    assert [e["step"] for e in store.get_journal(PLAN, STORY)[1]] == ["one", "two"]
    assert store.get_journal_final_ts(PLAN, STORY) == "t2"

    store.append_journal(PLAN, STORY, {"ts": "t3", "step": "three"})
    assert [e["step"] for e in store.get_journal(PLAN, STORY)[1]] == [
        "one",
        "two",
        "three",
    ]
    assert store.get_journal_final_ts(PLAN, STORY) == "t3"


def test_journal_entries_are_per_story(store_factory):
    """One story's journal never leaks into another story's."""
    store = store_factory()
    store.append_journal(PLAN, "S1", {"ts": "t1", "step": "one"})

    assert store.get_journal(PLAN, "S2") == (False, [])
    assert store.get_journal_final_ts(PLAN, "S2") is None


def test_list_manifests_and_list_plans(store_factory):
    """Both listings reflect what was saved, and are empty on a fresh store."""
    store = store_factory()

    assert store.list_manifests() == []
    assert store.list_plans() == []

    store.save_manifest("beta", EMPTY)
    store.save_manifest("alpha", EMPTY)

    assert store.list_manifests() == ["alpha", "beta"]
    assert "alpha" in store.list_plans()
    assert "alpha.manifest" not in store.list_plans()


def test_notifications_missing_and_appended(store_factory):
    """Notification readers fail open and read appends in order."""
    store = store_factory()

    assert store.get_notifications(PLAN) == []
    assert store.get_notification_records(PLAN) == []

    if isinstance(store, SqliteStore):
        # SqliteStore serves the legacy readers from its own table, so the
        # contract seeds through the store's writer, not file sidecars it no
        # longer reads. A row store has no unparseable lines to skip.
        store.records().append_notification(
            DEFAULT_NAMESPACE,
            PLAN,
            {"ts": "t1", "message": "m1", "severity": "warning", "story_key": "S1"},
        )

        assert store.get_notifications(PLAN) == ["t1 m1"]
        got = store.get_notification_records(PLAN)
        assert len(got) == 1
        assert got[0]["message"] == "m1"
        assert got[0]["severity"] == "warning"
        assert got[0]["story_key"] == "S1"
        return

    log = _plans_dir() / f"{PLAN}.notifications.log"
    log.write_text("first\nsecond\n")
    records = _plans_dir() / f"{PLAN}.notifications.jsonl"
    records.write_text(
        '{"ts": "t1", "message": "m1", "severity": "warning", "story_key": "S1"}\n'
        "not json\n"
    )

    assert store.get_notifications(PLAN) == ["first", "second"]
    got = store.get_notification_records(PLAN)
    assert len(got) == 1, "unparseable records are skipped, not raised"
    assert got[0]["message"] == "m1"
    assert got[0]["severity"] == "warning"
    assert got[0]["story_key"] == "S1"


def test_store_protocol_declares_no_workspace_read(store_factory):
    """The Store protocol exposes no workspace read (ADR 0012 decision 1).

    The story log and the worktree files are read through the
    ``WorkspaceReader`` seam, not through the store, so no control-plane
    method returns or reads a filesystem path.
    """
    from pipeline.store import Store

    store = store_factory()

    for name in ("get_story_log", "get_worktree_file"):
        assert not hasattr(Store, name), f"Store still declares {name}"
        assert not hasattr(store, name), f"{type(store).__name__} still exposes {name}"


def test_recent_workspaces_add_dedupes_and_prepends(monkeypatch, tmp_path):
    """The most recent workspace is first, and re-adding it does not duplicate.

    Moved off the store onto the host state (ADR 0012, decision 4); the store
    contract no longer covers workspace records.
    """
    from pipeline.host_state import FileHostState

    monkeypatch.setattr(pipeline.server, "PLAN_DIR", tmp_path)
    store = FileHostState()

    assert store.get_recent_workspaces() == []

    store.add_recent_workspace("/repo/one")
    store.add_recent_workspace("/repo/two")
    store.add_recent_workspace("/repo/one")

    assert store.get_recent_workspaces() == ["/repo/one", "/repo/two"]


def test_active_workspace_round_trip_and_clear(monkeypatch, tmp_path):
    """The active workspace persists, and clearing it reads back as None.

    Host-local record now (ADR 0012, decision 4), not part of the store
    contract.
    """
    from pipeline.host_state import FileHostState

    monkeypatch.setattr(pipeline.server, "PLAN_DIR", tmp_path)
    store = FileHostState()

    assert store.get_active_workspace() is None

    store.set_active_workspace("/repo/one")
    assert store.get_active_workspace() == "/repo/one"

    store.set_active_workspace(None)
    assert store.get_active_workspace() is None


def test_store_contract_declares_no_workspace_records(store_factory):
    """The store exposes no workspace record methods any more."""
    store = store_factory()
    for name in (
        "get_recent_workspaces",
        "add_recent_workspace",
        "get_active_workspace",
        "set_active_workspace",
    ):
        assert not hasattr(Store, name), f"Store still declares {name}"
        assert not hasattr(store, name), f"{type(store).__name__} still exposes {name}"


def test_get_manifest_or_none_reads_saved_and_swallows_corrupt(store_factory):
    """The fail-open manifest reader returns the document, or None on damage."""
    store = store_factory()
    if isinstance(store, SqliteStore):
        pytest.skip("a damaged SqliteStore row is covered in test_store_sqlite.py")
    store.save_manifest(PLAN, M1)

    assert store.get_manifest_or_none(PLAN) == M1

    (pipeline.server.PLAN_DIR / f"{PLAN}.manifest.json").write_text("{not json")

    assert store.get_manifest_or_none(PLAN) is None
    with pytest.raises(json.JSONDecodeError):
        store.get_manifest(PLAN)


def test_update_story_missing_manifest_raises(store_factory):
    """Updating a story in an unsaved plan reports the missing manifest."""
    store = store_factory()

    with pytest.raises(FileNotFoundError):
        store.update_story(PLAN, STORY, {"status": "running"})




# ---------------------------------------------------------------------------
# The notification-history contract.
#
# The notification history is state that survives every call: an appended
# notification is still there on the next read, a read never clears or
# consumes it, and the entries keep their append order.
#
# The behaviour lives in the production contract module
# ``pipeline.store_contract_notifications`` (the same pattern the sibling
# ``pipeline.store_contract_transactions`` uses): one copy of the contract, run
# against all three record store implementations through the classes below.
# The cases only the file-backed store can show -- the two on-disk
# representations, their bytes and the malformed-line count -- follow the
# classes.
# ---------------------------------------------------------------------------

NOTIFICATION_PLAN = "notification-plan"
FILE_PLAN = "file-notification-plan"

FILE_NOTIFICATION = {
    "plan": FILE_PLAN,
    "ts": "2026-01-01T00:00:00+00:00",
    "message": "a notification",
    "story_key": "story-a",
    "severity": "warning",
    "event": "story_parked",
    "dedup_key": "story-a:story_parked",
}


class TestFileRecordsNotificationHistory(NotificationHistoryContract):
    """The notification-history contract, run against the file store."""

    @pytest.fixture(autouse=True)
    def _plans_dir(self, tmp_path, monkeypatch):
        """An empty plans directory: the file store's history is on disk."""
        plan_dir = tmp_path / "plans"
        plan_dir.mkdir()
        monkeypatch.setattr(pipeline.server, "PLAN_DIR", plan_dir)
        monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", plan_dir)

    def make_store(self):
        return FileStore().records()


class TestSqliteRecordsNotificationHistory(NotificationHistoryContract):
    """The notification-history contract, run against the sqlite store.

    The sqlite store's record API is a view of the file store's, so it needs
    the same isolated plans directory.
    """

    @pytest.fixture(autouse=True)
    def _store(self, tmp_path, monkeypatch):
        """A ``records()`` view on a fresh database, closed after the test."""
        plan_dir = tmp_path / "plans"
        plan_dir.mkdir()
        monkeypatch.setattr(pipeline.server, "PLAN_DIR", plan_dir)
        monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", plan_dir)
        store = SqliteStore(str(tmp_path / "notifications.db"))
        self._store = store.records()
        try:
            yield self._store
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                close()

    def make_store(self):
        return self._store


class TestInMemoryRecordStoreNotificationHistory(NotificationHistoryContract):
    """The notification-history contract, run against the in-memory store."""

    serves_other_namespaces = True

    def make_store(self):
        return InMemoryRecordStore()


@pytest.fixture
def notification_plan_dir(tmp_path, monkeypatch):
    """Point every notification path at an empty plans directory."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", plan_dir)
    return plan_dir


def _expected_notification(event_record):
    """Build a record exactly as the bus sink does for the same fields."""
    return pipeline.persistence._notification_record(
        plan_name=event_record["plan"],
        message=event_record["message"],
        story_key=event_record["story_key"],
        severity=event_record["severity"],
        event=event_record["event"],
        dedup_key=event_record["dedup_key"],
        ts=event_record["ts"],
    )


def test_file_store_writes_both_representations(
    tmp_path, notification_plan_dir
):
    """One record becomes the ``.log`` line and the ``.jsonl`` line."""
    record = _expected_notification(FILE_NOTIFICATION)

    FileStore().records().append_notification(
        DEFAULT_NAMESPACE, FILE_PLAN, record
    )

    log = notification_plan_dir / f"{FILE_PLAN}.notifications.log"
    sidecar = notification_plan_dir / f"{FILE_PLAN}.notifications.jsonl"
    assert log.read_text() == f"{record['ts']} {record['message']}\n"
    assert sidecar.read_text() == json.dumps(record) + "\n"


def test_file_store_bytes_match_the_bus_sink(tmp_path, notification_plan_dir):
    """A store append is byte-identical to what ``file_log_sink`` writes."""
    from pipeline import notification_sinks

    record = _expected_notification(FILE_NOTIFICATION)
    sink_dir = tmp_path / "sink-plans"
    sink_dir.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pipeline.persistence, "PLAN_DIR", sink_dir)
        notification_sinks.file_log_sink(
            {
                "plan": record["plan"],
                "ts": record["ts"],
                "payload": {
                    "message": record["message"],
                    "story_key": record["story_key"],
                    "severity": record["severity"],
                    "event": record["event"],
                    "dedup_key": record["dedup_key"],
                },
            }
        )

    FileStore().records().append_notification(
        DEFAULT_NAMESPACE, FILE_PLAN, record
    )

    for suffix in (".notifications.log", ".notifications.jsonl"):
        written = (notification_plan_dir / f"{FILE_PLAN}{suffix}").read_bytes()
        sunk = (sink_dir / f"{FILE_PLAN}{suffix}").read_bytes()
        assert written == sunk, suffix


def test_file_store_counts_and_skips_a_malformed_line(notification_plan_dir):
    """An unreadable JSONL line is counted, and skipped, not served."""
    record = _expected_notification(FILE_NOTIFICATION)
    records = FileStore().records()
    records.append_notification(DEFAULT_NAMESPACE, FILE_PLAN, record)
    sidecar = notification_plan_dir / f"{FILE_PLAN}.notifications.jsonl"
    with open(sidecar, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    history, malformed = records.get_notification_history(
        DEFAULT_NAMESPACE, FILE_PLAN
    )

    assert malformed == 1
    assert history == [record]


def test_file_store_creates_nothing_for_a_plan_never_written(
    notification_plan_dir,
):
    """A read of a plan nobody notified is ``([], 0)`` and creates no file."""
    records = FileStore().records()

    assert records.get_notification_history(
        DEFAULT_NAMESPACE, "never-written"
    ) == ([], 0)
    assert (
        records.get_notification_lines(DEFAULT_NAMESPACE, "never-written") == []
    )
    assert list(notification_plan_dir.iterdir()) == []


def test_file_store_refused_record_writes_neither_file(notification_plan_dir):
    """A record with no ``ts``/``message`` is refused before anything is written."""
    records = FileStore().records()

    with pytest.raises(TypeError):
        records.append_notification(
            DEFAULT_NAMESPACE, FILE_PLAN, {"message": "no timestamp"}
        )
    with pytest.raises(TypeError):
        records.append_notification(
            DEFAULT_NAMESPACE, FILE_PLAN, {"ts": "2026-01-01T00:00:00+00:00"}
        )

    assert list(notification_plan_dir.iterdir()) == []
