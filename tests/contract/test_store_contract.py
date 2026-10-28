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
from pipeline.store import FileStore

PLAN = "contract-plan"


def _plans_dir(store):
    """The directory holding the plan files this store reads and writes.

    Derived from the store's own ``manifest_path`` (part of the Protocol) so
    tests seed raw log files where that backend will look for them instead of
    hard-coding a layout.
    """
    return store.manifest_path(PLAN).parent


@pytest.fixture(params=["file"], ids=["file"])
def store_factory(tmp_path, monkeypatch):
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
        store = FileStore()
        created.append(store)
        return store

    yield _make
    for store in created:
        if hasattr(store, "close"):
            store.close()


def test_manifest_path_locates_plan_manifest(store_factory):
    """``manifest_path`` is a pure path computation for the plan's manifest."""
    store = store_factory()

    path = store.manifest_path(PLAN)

    assert PLAN in path.name
    assert path == store.manifest_path(PLAN)
    assert not path.exists(), "computing a manifest path must not create the file"


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
    assert store.manifest_path(PLAN).exists()


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
    assert "alpha.manifest" in store.list_plans()


def test_notifications_missing_and_appended(store_factory):
    """Notification readers fail open and read appends in order."""
    store = store_factory()

    assert store.get_notifications(PLAN) == []
    assert store.get_notification_records(PLAN) == []

    log = _plans_dir(store) / f"{PLAN}.notifications.log"
    log.write_text("first\nsecond\n")
    records = _plans_dir(store) / f"{PLAN}.notifications.jsonl"
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


def test_get_story_log_missing_and_present(store_factory):
    """A story log is read relative to the plans directory, fail-open."""
    store = store_factory()

    assert store.get_story_log(PLAN, STORY, EMPTY) == {"available": False, "lines": []}

    (_plans_dir(store) / f"{PLAN}.{STORY}.log").write_text("a\nb\nc\n")
    manifest = {"stories": {STORY: {"log": f"{PLAN}.{STORY}.log"}}}

    got = store.get_story_log(PLAN, STORY, manifest, lines=2)

    assert got == {"available": True, "lines": ["b", "c"]}


def test_get_worktree_file_rejects_unsafe_input(store_factory):
    """Only a plain filename inside an absolute worktree is readable."""
    store = store_factory()
    empty = {"available": False, "text": ""}

    assert store.get_worktree_file({}, "notes.md") == empty
    assert store.get_worktree_file("not-a-dict", "notes.md") == empty
    assert store.get_worktree_file({"worktree": "relative/path"}, "notes.md") == empty
    assert store.get_worktree_file({"worktree": "/tmp"}, "../escape") == empty
    assert store.get_worktree_file({"worktree": "/tmp"}, "") == empty


def test_recent_workspaces_add_dedupes_and_prepends(store_factory):
    """The most recent workspace is first, and re-adding it does not duplicate."""
    store = store_factory()

    assert store.get_recent_workspaces() == []

    store.add_recent_workspace("/repo/one")
    store.add_recent_workspace("/repo/two")
    store.add_recent_workspace("/repo/one")

    assert store.get_recent_workspaces() == ["/repo/one", "/repo/two"]


def test_active_workspace_round_trip_and_clear(store_factory):
    """The active workspace persists, and clearing it reads back as None."""
    store = store_factory()

    assert store.get_active_workspace() is None

    store.set_active_workspace("/repo/one")
    assert store.get_active_workspace() == "/repo/one"

    store.set_active_workspace(None)
    assert store.get_active_workspace() is None


def test_get_manifest_or_none_reads_saved_and_swallows_corrupt(store_factory):
    """The fail-open manifest reader returns the document, or None on damage."""
    store = store_factory()
    store.save_manifest(PLAN, M1)

    assert store.get_manifest_or_none(PLAN) == M1

    store.manifest_path(PLAN).write_text("{not json")

    assert store.get_manifest_or_none(PLAN) is None
    with pytest.raises(json.JSONDecodeError):
        store.get_manifest(PLAN)


def test_update_story_missing_manifest_raises(store_factory):
    """Updating a story in an unsaved plan reports the missing manifest."""
    store = store_factory()

    with pytest.raises(FileNotFoundError):
        store.update_story(PLAN, STORY, {"status": "running"})


def test_get_worktree_file_reads_plain_file_inside_worktree(store_factory, tmp_path, monkeypatch):
    """A plain filename inside the configured worktree root is served."""
    store = store_factory()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "notes.md").write_text("hello\n")
    monkeypatch.setattr(pipeline.server, "WORKTREE_ROOT", tmp_path)

    got = store.get_worktree_file({"worktree": str(worktree)}, "notes.md")

    assert got == {"available": True, "text": "hello\n"}
    assert store.get_worktree_file({"worktree": str(worktree)}, "absent.md")[
        "available"
    ] is False


def test_get_story_log_rejects_absolute_path(store_factory, tmp_path):
    """An absolute log path in a manifest is treated as corrupt, not followed."""
    store = store_factory()
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n")
    manifest = {"stories": {STORY: {"log": str(outside)}}}

    assert store.get_story_log(PLAN, STORY, manifest) == {
        "available": False,
        "lines": [],
    }
