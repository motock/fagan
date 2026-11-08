"""``event_wiring.wake_handler`` reads its manifest through the record store.

``wake_handler`` used to load ``PLAN_DIR / "<plan>.manifest.json"`` itself, so
under ``PIPELINE_STORE=sqlite`` the ``agent_done`` precondition was decided from
this host's plan directory instead of the database's record. It now reads
through :func:`pipeline.manifest_source.read_plan_manifest` - the single
store-first manifest read (ADR 0012 stage B) - with this handler's existing
failure contract: an unreadable manifest is a refusal (``no_manifest``), never
an exception on the delivering thread, because a skip costs one wake and the
advance re-reads the manifest before it acts.

Nothing here edits an existing test: the fallback shapes (no record view, a
record view that does not know the plan, a store read that raises) are the ones
``tests/unit/test_manifest_source_store_first_read.py`` documents for the shared
reader, asserted here from the handler's own return value.
"""

from __future__ import annotations

import json

import pytest

from pipeline import event_wiring
from pipeline import server as p
from pipeline.event_guards import check_precondition
from pipeline.event_wiring import wake_handler


class _FakeRecordStore:
    """A store whose only useful surface is the single-manifest record read."""

    def __init__(self, manifests):
        self._manifests = manifests
        self.requested = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        self.requested.append((namespace, plan_name))
        return self._manifests.get(plan_name)

    def get_manifests(self, namespace):
        assert namespace == "default"
        return dict(self._manifests)


class _RaisingRecordStore(_FakeRecordStore):
    """A record view whose read fails - the store is unreachable, not empty."""

    def get_manifest(self, namespace, plan_name):
        raise RuntimeError("store unavailable")


class _LegacyStore:
    """A store with no record view at all (an in-process stand-in)."""

    def manifest_path(self, plan_name):
        raise AssertionError("the legacy store must not be consulted for records")


@pytest.fixture
def seams(tmp_path, monkeypatch):
    """Isolate the plan directory, the store and the advance side effect."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr("pipeline.paths.PLAN_DIR", plan_dir)
    monkeypatch.setattr(event_wiring, "PLAN_DIR", plan_dir)

    advanced = []

    def fake_advance(plan_name):
        advanced.append(plan_name)
        return {"ok": True, "dispatched": 1}

    monkeypatch.setattr(p, "advance_pipeline", fake_advance)

    # The hook is module-global; never let one test's hook leak into the next.
    event_wiring.set_wake_hook(None)
    yield {"plan_dir": plan_dir, "advanced": advanced}
    event_wiring.set_wake_hook(None)


def _event(plan="current", story_key="story-a", etype="agent_done"):
    return {"type": etype, "plan": plan, "story_key": story_key, "payload": {}}


def _manifest(status="in_progress"):
    return {"plan": "current", "stories": {"story-a": {"status": status}}}


def test_should_read_the_manifest_from_the_record_store_not_the_plan_dir(seams, monkeypatch):
    """Nothing is on disk; the store alone must wake the plan."""
    fake = _FakeRecordStore({"current": _manifest()})
    monkeypatch.setattr(p, "_store", fake)

    result = wake_handler(_event())

    assert result["ok"] is True
    assert result["woke"] == "current"
    assert seams["advanced"] == ["current"]
    assert fake.requested == [("default", "current")]
    assert not (seams["plan_dir"] / "current.manifest.json").exists()


def test_should_decide_the_precondition_from_the_store_manifest(seams, monkeypatch):
    """A story the store already has as ``done`` refuses the wake silently."""
    manifest = _manifest(status="done")
    monkeypatch.setattr(p, "_store", _FakeRecordStore({"current": manifest}))

    result = wake_handler(_event())

    assert result == check_precondition(manifest, "story-a", "agent_done")
    assert result["ok"] is False
    assert seams["advanced"] == []


def test_should_fall_back_to_the_plan_dir_for_a_store_with_no_record_view(seams, monkeypatch):
    """An in-process stand-in without ``records()`` keeps the legacy read."""
    (seams["plan_dir"] / "current.manifest.json").write_text(json.dumps(_manifest()))
    monkeypatch.setattr(p, "_store", _LegacyStore())

    result = wake_handler(_event())

    assert result["ok"] is True
    assert seams["advanced"] == ["current"]


def test_should_fall_back_to_the_plan_dir_when_the_store_has_no_record(seams, monkeypatch):
    """A record view that does not know the plan must not hide the legacy file."""
    (seams["plan_dir"] / "current.manifest.json").write_text(json.dumps(_manifest()))
    fake = _FakeRecordStore({})
    monkeypatch.setattr(p, "_store", fake)

    result = wake_handler(_event())

    assert result["ok"] is True
    assert fake.requested == [("default", "current")]
    assert seams["advanced"] == ["current"]


def test_a_failed_store_read_is_a_refusal_not_an_exception(seams, monkeypatch):
    """``fallback_on_error=True``: the handler refuses, the bus thread survives."""
    monkeypatch.setattr(p, "_store", _RaisingRecordStore({}))

    result = wake_handler(_event())

    assert result == {"ok": False, "skipped": "no_manifest"}
    assert seams["advanced"] == []


def test_a_non_object_manifest_record_falls_back_to_the_plan_dir(seams, monkeypatch):
    """A record that is not a JSON object is no manifest; the file decides."""
    (seams["plan_dir"] / "current.manifest.json").write_text(json.dumps(_manifest()))
    monkeypatch.setattr(p, "_store", _FakeRecordStore({"current": ["not", "an", "object"]}))

    result = wake_handler(_event())

    assert result["ok"] is True
    assert seams["advanced"] == ["current"]


def test_a_missing_manifest_in_both_sources_is_no_manifest(seams, monkeypatch):
    """Neither source knows the plan: the handler refunds with no_manifest."""
    monkeypatch.setattr(p, "_store", _FakeRecordStore({}))

    result = wake_handler(_event())

    assert result == {"ok": False, "skipped": "no_manifest"}
    assert seams["advanced"] == []


def test_the_queue_hook_path_also_reads_the_store_manifest(seams, monkeypatch):
    """Queue-driven advancement still gates on the store's manifest."""
    monkeypatch.setattr(p, "_store", _FakeRecordStore({"current": _manifest()}))
    queued = []
    event_wiring.set_wake_hook(queued.append)

    result = wake_handler(_event())

    assert result == {"ok": True, "queued": "current"}
    assert queued == ["current"]
    assert seams["advanced"] == []
