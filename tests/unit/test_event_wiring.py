"""Tests for ``pipeline.event_wiring``.

These tests are written FIRST (TDD). They exercise the contract described in
the ``event-driven-pipeline`` wiring story:

* ``wake_handler(event)`` reads a plan manifest from ``PLAN_DIR``, applies
  ``check_precondition``, and -- only when the precondition holds -- calls
  ``advance_pipeline`` (imported lazily) to wake the orchestration tick for
  that one plan.
* ``build_bus()`` returns an :class:`InProcessEventBus` with ``wake_handler``
  subscribed to the ``agent_done`` event type only.

The implementation module ``pipeline.event_wiring`` does not exist yet, so
importing it must fail -- that is the correct RED state for this dispatch.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from pipeline import event_wiring
from pipeline.event_wiring import build_bus, wake_handler

# The module under test does not exist yet, so the imports above fail -- that
# is the intended RED state for this dispatch.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manifest(plan: str, stories: dict) -> dict:
    """Return a manifest dict shaped like the real pipeline manifest."""
    return {"plan": plan, "stories": stories}


def _write_manifest(tmp_path, plan: str, manifest: dict) -> None:
    """Write a manifest JSON file for *plan* into *tmp_path* (patched PLAN_DIR)."""
    (tmp_path / f"{plan}.manifest.json").write_text(json.dumps(manifest))


def _event(plan: str = "p1", story_key: str = "S1", etype: str = "agent_done") -> dict:
    """Build a minimal event dict that wake_handler consumes."""
    return {"type": etype, "plan": plan, "story_key": story_key, "payload": {}}


@pytest.fixture
def patched_plan_dir(tmp_path, monkeypatch):
    """Patch ``PLAN_DIR`` in both ``pipeline.paths`` and ``event_wiring``."""
    monkeypatch.setattr("pipeline.paths.PLAN_DIR", tmp_path)
    # event_wiring imports PLAN_DIR from .paths at module load; patch the name
    # it actually references.
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def patched_advance(monkeypatch):
    """Patch ``advance_pipeline`` inside ``pipeline.server``.

    wake_handler imports it lazily as ``from .server import advance_pipeline``,
    so patching the attribute on ``pipeline.server`` is what the lazy import
    resolves at call time.
    """
    fn = mock.Mock(return_value={"ok": True, "dispatched": 1})
    monkeypatch.setattr("pipeline.server.advance_pipeline", fn)
    return fn


# ---------------------------------------------------------------------------
# Happy path: matching precondition calls advance_pipeline once
# ---------------------------------------------------------------------------

def test_matching_precondition_calls_advance_once(patched_plan_dir, patched_advance):
    """A story in an accepted status wakes the tick exactly once for the plan."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 1
    # Must be called with the event's plan name and nothing else.
    assert patched_advance.call_args == mock.call("p1")
    assert result == {"ok": True, "woke": "p1", "result": {"ok": True, "dispatched": 1}}


def test_matching_precondition_returns_advance_result_verbatim(
    patched_plan_dir, patched_advance
):
    """The 'result' field is exactly what advance_pipeline returned."""
    patched_advance.return_value = {"ok": True, "skipped": "locked", "extra": [1, 2]}
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert result["ok"] is True
    assert result["woke"] == "p1"
    assert result["result"] == {"ok": True, "skipped": "locked", "extra": [1, 2]}


# ---------------------------------------------------------------------------
# Negative: non-matching story status -> no advance, return skip record
# ---------------------------------------------------------------------------

def test_non_matching_status_does_not_advance(patched_plan_dir, patched_advance):
    """A story whose status is not accepted is a silent no-op."""
    # agent_done requires status "in_progress"; "todo" is not accepted.
    manifest = _manifest("p1", {"S1": {"status": "todo"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 0
    # The skip record from check_precondition is returned unchanged.
    assert result["ok"] is False
    assert result["skipped"] == "precondition_not_met"
    assert result["expected"] == ["in_progress"]
    assert result["actual"] == "todo"


def test_non_matching_status_returns_check_precondition_record_unchanged(
    patched_plan_dir, patched_advance
):
    """The returned dict is exactly the one check_precondition produced."""
    from pipeline.event_guards import check_precondition

    manifest = _manifest("p1", {"S1": {"status": "done"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    expected = check_precondition(manifest, "S1", "agent_done")
    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 0
    assert result == expected


# ---------------------------------------------------------------------------
# Negative: unknown story key -> no advance, skipped 'unknown_story'
# ---------------------------------------------------------------------------

def test_unknown_story_key_does_not_advance(patched_plan_dir, patched_advance):
    """An event for a story not in the manifest is a silent no-op."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    result = wake_handler(_event(plan="p1", story_key="NOPE"))

    assert patched_advance.call_count == 0
    assert result == {"ok": False, "skipped": "unknown_story"}


def test_missing_stories_mapping_is_unknown_story(patched_plan_dir, patched_advance):
    """A manifest with no 'stories' mapping is treated as unknown_story."""
    _write_manifest(patched_plan_dir, "p1", {"plan": "p1"})  # no 'stories'

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 0
    assert result == {"ok": False, "skipped": "unknown_story"}


# ---------------------------------------------------------------------------
# Negative: missing manifest file -> skipped 'no_manifest', no raise
# ---------------------------------------------------------------------------

def test_missing_manifest_returns_no_manifest(patched_plan_dir, patched_advance):
    """A missing manifest file is a WARNING-level no-op, never an exception."""
    # No file written for plan "ghost".
    result = wake_handler(_event(plan="ghost", story_key="S1"))

    assert patched_advance.call_count == 0
    assert result == {"ok": False, "skipped": "no_manifest"}


def test_missing_manifest_does_not_raise(patched_plan_dir, patched_advance):
    """wake_handler must not raise when the manifest is absent."""
    result = wake_handler(_event(plan="ghost", story_key="S1"))
    assert result == {"ok": False, "skipped": "no_manifest"}


# ---------------------------------------------------------------------------
# Negative: unparseable manifest -> skipped 'no_manifest', no raise
# ---------------------------------------------------------------------------

def test_unparseable_manifest_returns_no_manifest(patched_plan_dir, patched_advance):
    """A manifest file that is not valid JSON is a no-op, not an exception."""
    (patched_plan_dir / "p1.manifest.json").write_text("{ this is not : json")

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 0
    assert result == {"ok": False, "skipped": "no_manifest"}


def test_unparseable_manifest_does_not_raise(patched_plan_dir, patched_advance):
    """wake_handler must not raise when the manifest is unparseable."""
    (patched_plan_dir / "p1.manifest.json").write_text("not json at all {{{")
    try:
        result = wake_handler(_event(plan="p1", story_key="S1"))
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"wake_handler raised on unparseable manifest: {e!r}")
    assert result == {"ok": False, "skipped": "no_manifest"}


def test_empty_manifest_file_returns_no_manifest(patched_plan_dir, patched_advance):
    """An empty (zero-byte) manifest file is unparseable -> no_manifest."""
    (patched_plan_dir / "p1.manifest.json").write_text("")

    result = wake_handler(_event(plan="p1", story_key="S1"))

    assert patched_advance.call_count == 0
    assert result == {"ok": False, "skipped": "no_manifest"}


# ---------------------------------------------------------------------------
# advance_pipeline exceptions must NOT be swallowed
# ---------------------------------------------------------------------------

def test_advance_pipeline_exception_propagates(patched_plan_dir, monkeypatch):
    """wake_handler must not catch exceptions raised by advance_pipeline."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    def boom(plan_name):
        raise RuntimeError("orchestration exploded")

    monkeypatch.setattr("pipeline.server.advance_pipeline", boom)

    with pytest.raises(RuntimeError, match="orchestration exploded"):
        wake_handler(_event(plan="p1", story_key="S1"))


# ---------------------------------------------------------------------------
# Lazy import: wake_handler must import advance_pipeline lazily
# ---------------------------------------------------------------------------

def test_advance_pipeline_imported_lazily():
    """``pipeline.server.advance_pipeline`` must not be imported at module load.

    A module-level ``from .server import advance_pipeline`` would create a
    circular import. The implementation must import it inside wake_handler.
    """
    # The module must be importable without server.advance_pipeline being
    # resolved as a bound name at import time. We assert by checking that the
    # module does not hold a direct reference to the function object imported
    # eagerly; the only acceptable reference is the lazy lookup inside the
    # function body.
    import inspect

    src = inspect.getsource(event_wiring)
    # The import statement must appear inside a function body, not at module
    # top level. We check that 'from .server import advance_pipeline' (or an
    # equivalent) is present and that there is no module-level binding of
    # advance_pipeline.
    assert "advance_pipeline" in src, "wake_handler must reference advance_pipeline"
    # No module-level assignment/import of advance_pipeline: the name should
    # not be a module attribute.
    assert not hasattr(event_wiring, "advance_pipeline"), (
        "advance_pipeline must be imported lazily inside wake_handler, not "
        "bound at module level (circular import)."
    )


# ---------------------------------------------------------------------------
# build_bus
# ---------------------------------------------------------------------------

def test_build_bus_returns_in_process_bus():
    """build_bus returns an InProcessEventBus instance."""
    from pipeline.events import InProcessEventBus

    bus = build_bus()
    assert isinstance(bus, InProcessEventBus)


def test_build_bus_has_exactly_one_handler_for_agent_done():
    """Exactly one handler is registered for 'agent_done'."""
    bus = build_bus()
    handlers = bus._handlers.get("agent_done", [])
    assert len(handlers) == 1, f"expected 1 handler, got {handlers!r}"
    assert handlers[0] is wake_handler


def test_build_bus_subscribes_only_agent_done():
    """No other event type has any subscriber."""
    bus = build_bus()
    subscribed = {k for k, v in bus._handlers.items() if v}
    assert subscribed == {"agent_done"}, (
        f"only 'agent_done' should be subscribed, got {subscribed!r}"
    )


def test_build_bus_does_not_subscribe_other_known_types():
    """None of the other known event types have handlers."""
    from pipeline.events import EVENT_TYPES

    bus = build_bus()
    for etype in EVENT_TYPES:
        if etype == "agent_done":
            continue
        assert bus._handlers.get(etype, []) == [], (
            f"unexpected handler(s) for {etype!r}"
        )


# ---------------------------------------------------------------------------
# Publishing through the built bus
# ---------------------------------------------------------------------------

def test_publish_agent_done_reaches_wake_handler(monkeypatch, tmp_path):
    """Publishing an agent_done event through the built bus calls wake_handler."""
    monkeypatch.setattr("pipeline.paths.PLAN_DIR", tmp_path)
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    _write_manifest(tmp_path, "p1", _manifest("p1", {"S1": {"status": "in_progress"}}))

    fn = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr("pipeline.server.advance_pipeline", fn)

    bus = build_bus()
    from pipeline.events import make_event

    bus.publish(make_event("agent_done", "p1", story_key="S1"))

    assert fn.call_count == 1
    assert fn.call_args == mock.call("p1")


def test_publish_unsubscribed_type_does_not_raise(monkeypatch, tmp_path):
    """Publishing an event type with no subscriber is a silent no-op."""
    bus = build_bus()
    from pipeline.events import make_event

    # 'story_ready' has no subscriber on the built bus.
    try:
        bus.publish(make_event("story_ready", "p1", story_key="S1"))
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"publishing unsubscribed type raised: {e!r}")


def test_publish_unsubscribed_type_does_not_call_advance(monkeypatch, tmp_path):
    """An event type with no subscriber must not trigger advance_pipeline."""
    monkeypatch.setattr("pipeline.paths.PLAN_DIR", tmp_path)
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    _write_manifest(tmp_path, "p1", _manifest("p1", {"S1": {"status": "todo"}}))

    fn = mock.Mock()
    monkeypatch.setattr("pipeline.server.advance_pipeline", fn)

    bus = build_bus()
    from pipeline.events import make_event

    bus.publish(make_event("story_ready", "p1", story_key="S1"))
    assert fn.call_count == 0


# ---------------------------------------------------------------------------
# wake_handler must never invoke subprocess
# ---------------------------------------------------------------------------

def test_wake_handler_never_invokes_subprocess(patched_plan_dir, patched_advance, monkeypatch):
    """wake_handler must not shell out or run any subprocess."""
    fail = mock.Mock(side_effect=AssertionError("subprocess.run must not be called"))
    monkeypatch.setattr("subprocess.run", fail)
    # Also cover the common alias used in some call sites.
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", mock.Mock(side_effect=AssertionError("Popen must not be called")))

    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    # Happy path must not touch subprocess.
    wake_handler(_event(plan="p1", story_key="S1"))

    # Negative paths must not touch subprocess either.
    wake_handler(_event(plan="ghost", story_key="S1"))  # missing manifest
    (patched_plan_dir / "bad.manifest.json").write_text("not json")
    wake_handler(_event(plan="bad", story_key="S1"))  # unparseable manifest
    wake_handler(_event(plan="p1", story_key="NOPE"))  # unknown story


# ---------------------------------------------------------------------------
# Manifest is not mutated
# ---------------------------------------------------------------------------

def test_wake_handler_does_not_mutate_manifest(patched_plan_dir, patched_advance):
    """The manifest dict read from disk must not be mutated by wake_handler."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress", "extra": [1, 2]}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    # Re-read the manifest from disk to compare against post-call state.
    on_disk_before = json.loads(
        (patched_plan_dir / "p1.manifest.json").read_text()
    )

    wake_handler(_event(plan="p1", story_key="S1"))

    on_disk_after = json.loads(
        (patched_plan_dir / "p1.manifest.json").read_text()
    )
    assert on_disk_after == on_disk_before


def test_wake_handler_does_not_write_files(patched_plan_dir, patched_advance, monkeypatch):
    """wake_handler must not create or modify any files."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    files_before = {p.name for p in patched_plan_dir.iterdir()}

    wake_handler(_event(plan="p1", story_key="S1"))

    files_after = {p.name for p in patched_plan_dir.iterdir()}
    assert files_after == files_before, "wake_handler must not write any files"


# ---------------------------------------------------------------------------
# Missing required event fields
# ---------------------------------------------------------------------------

def test_wake_handler_missing_plan_key_raises(patched_plan_dir, patched_advance):
    """An event without a 'plan' key is a programming error, not a no-op."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    with pytest.raises(KeyError):
        wake_handler({"type": "agent_done", "story_key": "S1", "payload": {}})


def test_wake_handler_missing_story_key_raises(patched_plan_dir, patched_advance):
    """An event without a 'story_key' key is a programming error."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    with pytest.raises(KeyError):
        wake_handler({"type": "agent_done", "plan": "p1", "payload": {}})


def test_wake_handler_missing_type_raises(patched_plan_dir, patched_advance):
    """An event without a 'type' key is a programming error."""
    manifest = _manifest("p1", {"S1": {"status": "in_progress"}})
    _write_manifest(patched_plan_dir, "p1", manifest)

    with pytest.raises(KeyError):
        wake_handler({"plan": "p1", "story_key": "S1", "payload": {}})