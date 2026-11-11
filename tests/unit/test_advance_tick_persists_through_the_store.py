"""The advance tick persists its dispatch leases and review results through the
record store.

``_advance_pipeline_locked_impl`` used to re-read and write the manifest with
``json.loads(manifest_path.read_text())`` / ``_atomic_write_json(manifest_path,
m)`` around every ``_released_plan_lock`` window. Those re-reads must stay
FRESH (a new, independent copy read now - never ``get_manifests()``, whose
values are shared read-only cache snapshots), but they must go through the
record store like every other manifest reader/writer in the tick.

The fake store below is the seam under test: its ``get_manifest`` reflects its
``put_manifest``, and it keeps an authoritative in-memory copy that a second
process (another tick, a re-ingest) can advance *during* a released-lock
window without touching the on-disk projection. That mirrors
``PIPELINE_STORE=sqlite``, where the database row is authoritative and the
file projection can be stale.
"""

from __future__ import annotations

import json

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import concurrency
from pipeline import server as p
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name

_PLAN = "store-tick-plan"


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    """Point the real flock at tmp_path (mirrors test_released_plan_lock.py)."""
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


class _RecordingStore:
    """A fake record store whose ``get_manifest`` reflects ``put_manifest``.

    ``manifest`` is the record's authoritative copy. ``self.path`` is the
    file projection: :meth:`put_manifest` refreshes both, while
    :meth:`other_process_advances` - a second process acting during a
    released-lock window - updates only the authoritative copy, exactly as a
    stale file projection behaves under ``PIPELINE_STORE=sqlite``.
    """

    def __init__(self, path, manifest):
        self.path = path
        self.manifest = manifest
        self.events = []
        self.writes = []
        self.reads = 0
        self.vanish_on_read = None
        self.path.write_text(json.dumps(manifest))

    # -- store surface -------------------------------------------------
    def manifest_path(self, plan_name):
        return self.path

    def records(self):
        return self

    def list_plans(self, namespace):
        return [_PLAN] if self.manifest_path(_PLAN).exists() else []

    def get_manifests(self, namespace):
        return {}

    def get_manifest(self, *args):
        # One argument: the legacy Store-level read, which serves the file
        # projection. Two: the record view, which validates the plan name
        # exactly like FileRecords does and reads the authoritative record.
        if len(args) == 1:
            return json.loads(self.path.read_text())
        _namespace, plan_name = args
        validate_plan_name(plan_name)
        self.events.append(("get_manifest", plan_name))
        self.reads += 1
        if self.vanish_on_read is not None and self.reads >= self.vanish_on_read:
            # A re-ingest deleted the plan mid-tick: the record is gone and so
            # is the file projection.
            self.manifest = None
            self.path.unlink(missing_ok=True)
        if self.manifest is None:
            return None
        return json.loads(json.dumps(self.manifest))

    def put_manifest(self, namespace, plan_name, manifest):
        validate_plan_name(plan_name)
        self.events.append(("put_manifest", plan_name))
        self.writes.append(plan_name)
        self.manifest = json.loads(json.dumps(manifest))
        self.path.write_text(json.dumps(manifest))

    # -- test helper ---------------------------------------------------
    def other_process_advances(self, manifest):
        """A different process mutates the plan during a release window."""
        self.manifest = json.loads(json.dumps(manifest))


def _story(status="todo", **extra):
    story = {
        "key": "s1",
        "title": "story",
        "status": status,
        "risk": "low",
        "dispatch_attempts": 0,
    }
    story.update(extra)
    return story


def _manifest(stories):
    return {"name": _PLAN, "paused": False, "stories": stories}


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


def _seed(monkeypatch, store):
    """Wire every seam the tick body touches, wide open."""
    monkeypatch.setattr(advance_module, "_store", store)
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "_count_on_device_in_progress_agents", lambda: 0)
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})
    # Neither the per-tick cap nor the on-device slot math may become the
    # binding constraint: the freshness check under test is the only reason a
    # second story may be skipped.
    monkeypatch.setattr(config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 5, raising=False)
    monkeypatch.setattr(advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 5, raising=False)
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    for name in (
        "SESSION_PAUSE_THRESHOLD",
        "SESSION_RESUME_THRESHOLD",
        "WEEK_PAUSE_THRESHOLD",
        "WEEK_RESUME_THRESHOLD",
    ):
        monkeypatch.setattr(config_module, name, 101, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, 101, raising=False)


def _run_tick_holding_lock(plan_name=_PLAN):
    """Run the tick body under the real plan lock, as production does."""
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


class _DispatchRecorder:
    def __init__(self, *extra):
        self.calls = []
        self.extra = extra

    def __call__(self, plan_name, key):
        self.calls.append(key)
        for hook in self.extra:
            hook(plan_name, key)
        return {"ok": True}


# ---------------------------------------------------------------------------
# The dispatch lease must be persisted BEFORE the plan lock is released


def test_should_persist_the_dispatch_lease_before_releasing_the_lock(
    monkeypatch, tmp_path
):
    store = _RecordingStore(tmp_path / "manifest.json", _manifest({"s1": _story()}))
    _seed(monkeypatch, store)
    monkeypatch.setattr(advance_module, "dispatch_story", _DispatchRecorder())

    real_released = advance_module._released_plan_lock

    def _spy(plan_name):
        store.events.append(("enter_released_plan_lock", plan_name))
        return real_released(plan_name)

    monkeypatch.setattr(advance_module, "_released_plan_lock", _spy)

    _run_tick_holding_lock()

    kinds = [event[0] for event in store.events]
    assert "put_manifest" in kinds, (
        f"the dispatch lease must be persisted through the record store: {kinds}"
    )
    claim_at = kinds.index("put_manifest")
    assert "enter_released_plan_lock" in kinds, (
        f"the plan lock must still be released around dispatch_story: {kinds}"
    )
    release_at = kinds.index("enter_released_plan_lock")
    assert claim_at < release_at, (
        "the lease claim must be persisted BEFORE the plan lock is released - "
        f"an unpersisted lease protects nothing: {kinds}"
    )
    assert store.manifest["stories"]["s1"]["status"] == "todo"
    assert "dispatch_lease_expires_at" not in store.manifest["stories"]["s1"]


# ---------------------------------------------------------------------------
# Release-window re-reads: a story another actor moved is skipped


def test_should_skip_a_story_another_process_advanced_during_a_release_window(
    monkeypatch, tmp_path
):
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest({"s1": _story(), "s2": _story(key="s2")}),
    )
    _seed(monkeypatch, store)

    def _advance_s2_during_s1(plan_name, key):
        if key != "s1":
            return
        payload = json.loads(json.dumps(store.manifest))
        payload["stories"]["s2"].update(
            status="in_progress",
            pid=424242,
            dispatch_lease_owner_pid=424242,
            dispatch_lease_expires_at="2999-01-01T00:00:00+00:00",
        )
        store.other_process_advances(payload)

    dispatch = _DispatchRecorder(_advance_s2_during_s1)
    monkeypatch.setattr(advance_module, "dispatch_story", dispatch)

    _run_tick_holding_lock()

    assert dispatch.calls == ["s1"], (
        "s2 was advanced by another process during s1's release window and "
        f"must not be dispatched by this tick either: {dispatch.calls}"
    )
    final = store.manifest["stories"]["s2"]
    assert final["status"] == "in_progress"
    assert final["pid"] == 424242
    assert final["dispatch_lease_owner_pid"] == 424242, (
        f"the tick must not revert the other process's record: {final}"
    )


def test_should_skip_a_story_dropped_by_a_reingest_during_a_release_window(
    monkeypatch, tmp_path
):
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest({"s1": _story(), "s2": _story(key="s2")}),
    )
    _seed(monkeypatch, store)

    def _drop_s2_during_s1(plan_name, key):
        if key != "s1":
            return
        payload = json.loads(json.dumps(store.manifest))
        del payload["stories"]["s2"]
        store.other_process_advances(payload)

    dispatch = _DispatchRecorder(_drop_s2_during_s1)
    monkeypatch.setattr(advance_module, "dispatch_story", dispatch)

    _run_tick_holding_lock()

    assert dispatch.calls == ["s1"], (
        "s2 was dropped by a re-ingest during s1's release window and must not "
        f"be dispatched (nor KeyError the tick): {dispatch.calls}"
    )
    assert "s2" not in store.manifest["stories"], (
        "the tick must not resurrect a story the re-ingest dropped: "
        f"{store.manifest['stories']}"
    )
    # The follow-up tick sees the re-ingest's content, not a ghost stub.
    assert store.get_manifest("default", _PLAN)["stories"].keys() == {"s1"}


# ---------------------------------------------------------------------------
# A plan name the store refuses fails closed (no write, no traceback)


def test_should_fail_closed_when_the_plan_name_is_not_a_valid_record_key(
    monkeypatch, tmp_path
):
    store = _RecordingStore(tmp_path / "manifest.json", _manifest({"s1": _story()}))
    _seed(monkeypatch, store)
    monkeypatch.setattr(advance_module, "dispatch_story", _DispatchRecorder())

    with pytest.raises(InvalidRecordKeyError) as excinfo:
        _run_tick_holding_lock("a..b")

    assert store.writes == [], f"no manifest write may happen: {store.writes}"
    assert str(store.path) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Story B: the polling/escalation and review re-reads go through the store


def test_should_persist_an_escalation_fallback_through_the_record_store(
    monkeypatch, tmp_path
):
    store = _RecordingStore(
        tmp_path / "manifest.json",
        {
            "name": _PLAN,
            "paused": False,
            "local_model_fallback": "m2",
            "stories": {
                "s1": _story(
                    status="in_progress",
                    pid=4242,
                    backend="local",
                    model="m1",
                    escalated=True,
                )
            },
        },
    )
    _seed(monkeypatch, store)

    def _grade_and_record(plan_name, key):
        # check_story_status writes the story's terminal status via its own
        # store transaction, so the tick's top-of-tick copy is now stale.
        payload = json.loads(json.dumps(store.manifest))
        payload["stories"][key].update(
            status="failed", failure_kind="tests_failed", graded_by_poller=True
        )
        store.other_process_advances(payload)
        return {"status": "failed", "failure_kind": "tests_failed"}

    monkeypatch.setattr(advance_module, "check_story_status", _grade_and_record)

    escalate = _DispatchRecorder()

    def _escalate_to_fallback(manifest, plan_name, key, manifest_path, fallback_model):
        escalate.calls.append(json.loads(json.dumps(manifest)))
        story = manifest["stories"][key]
        story["model"] = fallback_model
        story["tried_fallback_model"] = True
        story["status"] = "todo"
        store.put_manifest("default", plan_name, manifest)

    monkeypatch.setattr(
        advance_module, "_escalate_to_local_fallback_model", _escalate_to_fallback
    )

    _run_tick_holding_lock()

    assert escalate.calls, "test setup: the fallback path was never reached"
    handed = escalate.calls[0]["stories"]["s1"]
    assert handed.get("graded_by_poller") is True, (
        "the fallback escalation must be handed a FRESH read of the record, not "
        f"the tick's stale top-of-loop copy: {handed}"
    )
    # Follow-up: the persisted record is what the next tick reads, so the
    # fallback model is not offered twice.
    persisted = store.get_manifest("default", _PLAN)["stories"]["s1"]
    assert persisted["model"] == "m2"
    assert persisted["tried_fallback_model"] is True
    assert persisted["graded_by_poller"] is True
    assert store.writes == [_PLAN], (
        f"the fallback must be persisted through the store: {store.writes}"
    )


def test_should_raise_and_not_write_when_the_manifest_vanished_mid_tick(
    monkeypatch, tmp_path
):
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest({"s1": _story(status="in_progress", pid=4242, model="m1")}),
    )
    _seed(monkeypatch, store)
    # Read #1 is the top-of-tick read; read #2 is the poll loop's fresh read.
    store.vanish_on_read = 2

    with pytest.raises(FileNotFoundError) as excinfo:
        _run_tick_holding_lock()

    assert "manifest" in str(excinfo.value)
    assert str(store.path) not in str(excinfo.value), (
        f"the message must not leak a path: {excinfo.value}"
    )
    assert store.writes == [], (
        f"a vanished manifest must never be written back: {store.writes}"
    )
    # Follow-up: the tick did not re-create the plan; the next tick sees it gone.
    assert store.path.exists() is False
    assert store.get_manifest("default", _PLAN) is None


def test_should_fail_closed_at_the_review_site_for_an_invalid_record_key(
    monkeypatch, tmp_path
):
    store = _RecordingStore(
        tmp_path / "manifest.json",
        _manifest({"s1": _story(status="tests_passed")}),
    )
    _seed(monkeypatch, store)
    monkeypatch.setattr(
        advance_module,
        "review_story",
        lambda *a, **k: pytest.fail("review_story must not run for an invalid key"),
    )

    with pytest.raises(InvalidRecordKeyError) as excinfo:
        _run_tick_holding_lock("a..b")

    assert store.writes == [], f"no manifest write may happen: {store.writes}"
    assert str(store.path) not in str(excinfo.value)
