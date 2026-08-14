"""Tests for pipeline.events - event construction and in-process event bus.

These tests are written FIRST (TDD). The implementation module
``pipeline.events`` does not exist yet, so this suite is expected to be RED
until a later dispatch implements it.
"""

import logging
from datetime import datetime

import pytest

from pipeline import events

# ---------------------------------------------------------------------------
# Module-level constant EVENT_TYPES
# ---------------------------------------------------------------------------

EXPECTED_EVENT_TYPES = frozenset(
    {
        "story_ready",
        "agent_dispatched",
        "agent_done",
        "tests_passed",
        "changes_requested",
        "pr_open",
        "ci_complete",
        "story_done",
        "parked",
        "rework",
        "reconcile",
        "notification",
    }
)


def test_event_types_is_frozenset():
    assert isinstance(events.EVENT_TYPES, frozenset)


def test_event_types_contains_exactly_expected_members():
    assert events.EVENT_TYPES == EXPECTED_EVENT_TYPES


# ---------------------------------------------------------------------------
# make_event
# ---------------------------------------------------------------------------


def test_make_event_returns_all_five_keys():
    ev = events.make_event("story_ready", "plan-1")
    assert set(ev.keys()) == {"type", "plan", "story_key", "payload", "ts"}


def test_make_event_values_populated():
    ev = events.make_event(
        "agent_done", "plan-1", story_key="S-3", payload={"x": 1}
    )
    assert ev["type"] == "agent_done"
    assert ev["plan"] == "plan-1"
    assert ev["story_key"] == "S-3"
    assert ev["payload"] == {"x": 1}


def test_make_event_ts_is_iso8601():
    ev = events.make_event("story_ready", "plan-1")
    # Must parse as ISO 8601 (datetime.fromisoformat handles the UTC form).
    parsed = datetime.fromisoformat(ev["ts"])
    assert parsed is not None
    # Should be timezone-aware (UTC).
    assert parsed.tzinfo is not None


def test_make_event_unknown_type_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("not_a_real_type", "plan-1")


def test_make_event_unknown_type_message_mentions_type():
    with pytest.raises(ValueError) as excinfo:
        events.make_event("not_a_real_type", "plan-1")
    assert "not_a_real_type" in str(excinfo.value)


def test_make_event_empty_plan_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("story_ready", "")


def test_make_event_none_plan_raises_value_error():
    with pytest.raises(ValueError):
        events.make_event("story_ready", None)


def test_make_event_defaults_payload_to_empty_dict():
    ev = events.make_event("story_ready", "plan-1")
    assert ev["payload"] == {}


def test_make_event_defaults_story_key_to_none():
    ev = events.make_event("story_ready", "plan-1")
    assert ev["story_key"] is None


def test_make_event_payload_none_defaults_to_empty_dict():
    ev = events.make_event("story_ready", "plan-1", payload=None)
    assert ev["payload"] == {}


def test_make_event_every_valid_type_accepted():
    for t in EXPECTED_EVENT_TYPES:
        ev = events.make_event(t, "plan-1")
        assert ev["type"] == t


# ---------------------------------------------------------------------------
# EventBus abstract base class
# ---------------------------------------------------------------------------


def test_eventbus_is_abstract():
    assert issubclass(events.InProcessEventBus, events.EventBus)
    # Cannot instantiate the abstract base directly.
    with pytest.raises(TypeError):
        events.EventBus()  # type: ignore[abstract]


def test_eventbus_has_publish_and_subscribe_abstract_methods():
    # The abstract methods must be declared on the base class.
    assert "publish" in events.EventBus.__abstractmethods__
    assert "subscribe" in events.EventBus.__abstractmethods__


# ---------------------------------------------------------------------------
# InProcessEventBus
# ---------------------------------------------------------------------------


def test_subscribe_and_publish_calls_handler_once():
    bus = events.InProcessEventBus()
    calls = []
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert calls == [ev]


def test_two_handlers_same_type_both_called():
    bus = events.InProcessEventBus()
    a = []
    b = []
    bus.subscribe("story_ready", a.append)
    bus.subscribe("story_ready", b.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert a == [ev]
    assert b == [ev]


def test_publish_no_subscribers_does_not_raise():
    bus = events.InProcessEventBus()
    ev = events.make_event("story_ready", "plan-1")
    # No subscribers registered for this type - silent no-op.
    bus.publish(ev)  # must not raise


def test_handler_that_raises_does_not_stop_others(caplog):
    bus = events.InProcessEventBus()

    second_called = []

    def bad_handler(_event):
        raise RuntimeError("boom")

    def good_handler(event):
        second_called.append(event)

    bus.subscribe("story_ready", bad_handler)
    bus.subscribe("story_ready", good_handler)

    ev = events.make_event("story_ready", "plan-1")
    with caplog.at_level(logging.ERROR, logger="pipeline.events"):
        # Must not propagate the bad handler's exception.
        bus.publish(ev)

    assert second_called == [ev]
    # The exception should have been logged at ERROR level.
    assert any(
        record.levelno == logging.ERROR and "boom" in record.getMessage()
        for record in caplog.records
    )


def test_publish_only_calls_handlers_for_matching_type():
    bus = events.InProcessEventBus()
    ready_calls = []
    done_calls = []
    bus.subscribe("story_ready", ready_calls.append)
    bus.subscribe("agent_done", done_calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    assert ready_calls == [ev]
    assert done_calls == []


def test_subscribe_appends_does_not_replace():
    bus = events.InProcessEventBus()
    calls = []
    bus.subscribe("story_ready", calls.append)
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    # Two subscriptions => handler called twice.
    assert calls == [ev, ev]


# ---------------------------------------------------------------------------
# JsonlEventBus
# ---------------------------------------------------------------------------
#
# A per-plan JSONL append-log backed event bus. These tests are written FIRST
# (TDD) and are expected to be RED until a later dispatch implements
# ``JsonlEventBus`` in pipeline/events.py.

import json
from pathlib import Path


def _jsonl_bus(tmp_path: Path) -> "events.JsonlEventBus":
    """Construct a JsonlEventBus rooted at tmp_path."""
    return events.JsonlEventBus(tmp_path)


def _plan_log_path(root: Path, plan: str) -> Path:
    """The expected log file path for a plan."""
    return root / f"{plan}.events.jsonl"


def test_jsonl_event_bus_is_subclass_of_event_bus():
    assert issubclass(events.JsonlEventBus, events.EventBus)


def test_jsonl_event_bus_constructor_takes_directory(tmp_path):
    # Constructor accepts a single Path argument (the mailbox root).
    bus = events.JsonlEventBus(tmp_path)
    assert bus is not None


def test_publish_writes_exactly_one_line(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    log = _plan_log_path(tmp_path, "plan-1")
    assert log.exists()
    content = log.read_text(encoding="utf-8")
    # Exactly one line, terminated by a single newline.
    assert content.count("\n") == 1
    assert content.endswith("\n")
    line = content.rstrip("\n")
    assert line != ""


def test_two_publishes_write_two_lines(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev1 = events.make_event("story_ready", "plan-1")
    ev2 = events.make_event("agent_done", "plan-1")
    bus.publish(ev1)
    bus.publish(ev2)
    log = _plan_log_path(tmp_path, "plan-1")
    content = log.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) == 2
    # Each line is exactly one record (no embedded newlines).
    for line in lines:
        assert "\n" not in line


def test_written_line_round_trips_through_json_loads(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev = events.make_event(
        "agent_done", "plan-1", story_key="S-9", payload={"k": [1, 2, 3]}
    )
    bus.publish(ev)
    log = _plan_log_path(tmp_path, "plan-1")
    line = log.read_text(encoding="utf-8").rstrip("\n")
    parsed = json.loads(line)
    assert parsed == ev


def test_publish_creates_parent_directory_when_missing(tmp_path):
    # A nested root that does not yet exist.
    root = tmp_path / "does_not_exist_yet"
    assert not root.exists()
    bus = events.JsonlEventBus(root)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    # publish must have created the parent directory.
    assert root.exists()
    assert _plan_log_path(root, "plan-1").exists()


def test_publish_appends_does_not_overwrite(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev1 = events.make_event("story_ready", "plan-1")
    ev2 = events.make_event("agent_done", "plan-1")
    bus.publish(ev1)
    bus.publish(ev2)
    log = _plan_log_path(tmp_path, "plan-1")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == ev1
    assert json.loads(lines[1]) == ev2


def test_subscribe_does_not_read_file(tmp_path):
    # Pre-seed a log file on disk; subscribe must not consume or touch it.
    log = _plan_log_path(tmp_path, "plan-1")
    log.write_text(json.dumps({"type": "story_ready", "plan": "plan-1"}) + "\n",
                   encoding="utf-8")
    before = log.read_text(encoding="utf-8")
    bus = _jsonl_bus(tmp_path)
    calls = []
    bus.subscribe("story_ready", calls.append)
    # No dispatch should have happened just from subscribing.
    assert calls == []
    # File contents untouched.
    assert log.read_text(encoding="utf-8") == before


def test_drain_dispatches_each_event_to_subscribed_handler_in_file_order(tmp_path):
    bus = _jsonl_bus(tmp_path)
    calls = []
    bus.subscribe("story_ready", calls.append)
    ev1 = events.make_event("story_ready", "plan-1")
    ev2 = events.make_event("story_ready", "plan-1")
    bus.publish(ev1)
    bus.publish(ev2)
    bus.drain("plan-1")
    assert calls == [ev1, ev2]


def test_drain_returns_parsed_events(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev1 = events.make_event("story_ready", "plan-1")
    ev2 = events.make_event("agent_done", "plan-1")
    bus.publish(ev1)
    bus.publish(ev2)
    result = bus.drain("plan-1")
    assert isinstance(result, list)
    assert result == [ev1, ev2]


def test_drain_no_subscribers_is_noop_and_returns_events(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    # No handlers registered at all.
    result = bus.drain("plan-1")
    assert result == [ev]


def test_drain_truncates_file_so_second_drain_returns_empty(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    first = bus.drain("plan-1")
    assert first == [ev]
    # File should now be empty.
    log = _plan_log_path(tmp_path, "plan-1")
    assert log.read_text(encoding="utf-8") == ""
    # A second drain returns nothing.
    second = bus.drain("plan-1")
    assert second == []


def test_drain_on_nonexistent_plan_returns_empty_and_does_not_raise(tmp_path):
    bus = _jsonl_bus(tmp_path)
    # No file written for this plan.
    assert not _plan_log_path(tmp_path, "ghost").exists()
    result = bus.drain("ghost")
    assert result == []
    # Must not have created the file as a side effect.
    assert not _plan_log_path(tmp_path, "ghost").exists()


def test_drain_malformed_line_skipped_and_remaining_valid_lines_dispatch(
    tmp_path, caplog
):
    bus = _jsonl_bus(tmp_path)
    calls = []
    bus.subscribe("story_ready", calls.append)

    ev1 = events.make_event("story_ready", "plan-1")
    ev3 = events.make_event("story_ready", "plan-1")

    # Manually craft a log with a malformed middle line.
    log = _plan_log_path(tmp_path, "plan-1")
    log.write_text(
        json.dumps(ev1) + "\n"
        + "this is not valid json\n"
        + json.dumps(ev3) + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="pipeline.events"):
        result = bus.drain("plan-1")

    # The two valid events were dispatched in order; the bad line skipped.
    assert calls == [ev1, ev3]
    assert result == [ev1, ev3]
    # The malformed line was logged at WARNING.
    assert any(
        record.levelno == logging.WARNING for record in caplog.records
    )


def test_drain_handler_that_raises_is_logged_and_does_not_stop_rest(tmp_path, caplog):
    bus = _jsonl_bus(tmp_path)
    seen = []

    def bad_handler(_event):
        raise RuntimeError("kaboom")

    def good_handler(event):
        seen.append(event)

    bus.subscribe("story_ready", bad_handler)
    bus.subscribe("story_ready", good_handler)

    ev1 = events.make_event("story_ready", "plan-1")
    ev2 = events.make_event("story_ready", "plan-1")
    bus.publish(ev1)
    bus.publish(ev2)

    with caplog.at_level(logging.ERROR, logger="pipeline.events"):
        result = bus.drain("plan-1")

    # good_handler still saw both events despite bad_handler raising.
    assert seen == [ev1, ev2]
    # All events still returned.
    assert result == [ev1, ev2]
    # The exception was logged at ERROR.
    assert any(
        record.levelno == logging.ERROR and "kaboom" in record.getMessage()
        for record in caplog.records
    )


def test_drain_only_dispatches_matching_event_type(tmp_path):
    bus = _jsonl_bus(tmp_path)
    ready_calls = []
    done_calls = []
    bus.subscribe("story_ready", ready_calls.append)
    bus.subscribe("agent_done", done_calls.append)

    ev_ready = events.make_event("story_ready", "plan-1")
    ev_done = events.make_event("agent_done", "plan-1")
    bus.publish(ev_ready)
    bus.publish(ev_done)

    bus.drain("plan-1")
    assert ready_calls == [ev_ready]
    assert done_calls == [ev_done]


def test_drain_multiple_handlers_same_type_both_called(tmp_path):
    bus = _jsonl_bus(tmp_path)
    a = []
    b = []
    bus.subscribe("story_ready", a.append)
    bus.subscribe("story_ready", b.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    bus.drain("plan-1")
    assert a == [ev]
    assert b == [ev]


def test_drain_empty_file_returns_empty(tmp_path):
    bus = _jsonl_bus(tmp_path)
    log = _plan_log_path(tmp_path, "plan-1")
    log.write_text("", encoding="utf-8")
    result = bus.drain("plan-1")
    assert result == []


def test_drain_single_event_boundary(tmp_path):
    bus = _jsonl_bus(tmp_path)
    calls = []
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    result = bus.drain("plan-1")
    assert result == [ev]
    assert calls == [ev]


def test_drain_isolates_plans_by_filename(tmp_path):
    bus = _jsonl_bus(tmp_path)
    plan_a_calls = []
    bus.subscribe("story_ready", plan_a_calls.append)
    ev_a = events.make_event("story_ready", "plan-A")
    ev_b = events.make_event("story_ready", "plan-B")
    bus.publish(ev_a)
    bus.publish(ev_b)

    # Draining plan-A must only dispatch plan-A's event.
    result_a = bus.drain("plan-A")
    assert result_a == [ev_a]
    assert plan_a_calls == [ev_a]

    # plan-B's file is untouched and still drainable.
    result_b = bus.drain("plan-B")
    assert result_b == [ev_b]


def test_subscribe_stores_handlers_in_dict_like_in_process_bus(tmp_path):
    # subscribe registration behavior mirrors InProcessEventBus: multiple
    # subscribes append handlers rather than replacing.
    bus = _jsonl_bus(tmp_path)
    calls = []
    bus.subscribe("story_ready", calls.append)
    bus.subscribe("story_ready", calls.append)
    ev = events.make_event("story_ready", "plan-1")
    bus.publish(ev)
    bus.drain("plan-1")
    # Two subscriptions => handler called twice per event.
    assert calls == [ev, ev]


# ---------------------------------------------------------------------------
# Regression: path traversal via unsanitized `plan` in publish/drain
# ---------------------------------------------------------------------------
# The `plan` identifier is interpolated raw into a filesystem path:
#     self._root / f"{plan}.events.jsonl"
# A plan containing "/" or ".." escapes the root on write (publish, mode "a")
# and on drain both reads and truncates (mode "w") that escaped path. These
# tests assert that publish/drain reject unsafe plan ids with ValueError and
# perform NO filesystem operation outside the root.

_TRAVERSAL_PLANS = [
    None,
    "",
    "   ",
    "../evil",
    "a/b",
    "..\\evil",
    "sub/../../escape",
    "..",
    ".",
]


def _event_with_plan(plan):
    """Build a raw event dict carrying an arbitrary (possibly unsafe) plan.

    Unlike ``make_event`` (which only rejects empty/None), this bypasses event
    construction validation so the *publish* path-validation is exercised
    directly.
    """
    return {
        "type": "story_ready",
        "plan": plan,
        "story_key": None,
        "payload": {},
        "ts": "2024-01-01T00:00:00+00:00",
    }


def _files_outside_root(tmp_path: Path, root: Path) -> dict:
    """Snapshot {path: bytes} for every file under tmp_path but outside root."""
    root_resolved = root.resolve()
    snap: dict = {}
    for p in tmp_path.rglob("*"):
        if not p.is_file():
            continue
        resolved = p.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            continue
        snap[p] = p.read_bytes()
    return snap


@pytest.mark.parametrize("plan", _TRAVERSAL_PLANS)
def test_publish_rejects_unsafe_plan_with_value_error(tmp_path, plan):
    root = tmp_path / "root"
    bus = events.JsonlEventBus(root)
    before = _files_outside_root(tmp_path, root)
    with pytest.raises(ValueError):
        bus.publish(_event_with_plan(plan))
    after = _files_outside_root(tmp_path, root)
    assert after == before, (
        f"publish created/modified files outside root for plan={plan!r}"
    )


@pytest.mark.parametrize("plan", _TRAVERSAL_PLANS)
def test_drain_rejects_unsafe_plan_with_value_error(tmp_path, plan):
    root = tmp_path / "root"
    bus = events.JsonlEventBus(root)
    before = _files_outside_root(tmp_path, root)
    with pytest.raises(ValueError):
        bus.drain(plan)
    after = _files_outside_root(tmp_path, root)
    assert after == before, (
        f"drain created/modified files outside root for plan={plan!r}"
    )


def test_publish_traversal_does_not_truncate_victim_file(tmp_path):
    # A pre-existing file outside the root that "../victim" would resolve to.
    root = tmp_path / "root"
    victim = tmp_path / "victim.events.jsonl"
    victim.write_text("precious", encoding="utf-8")
    bus = events.JsonlEventBus(root)
    with pytest.raises(ValueError):
        bus.publish(_event_with_plan("../victim"))
    # The victim file must be untouched.
    assert victim.read_text(encoding="utf-8") == "precious"


def test_drain_traversal_does_not_truncate_victim_file(tmp_path):
    root = tmp_path / "root"
    victim = tmp_path / "victim.events.jsonl"
    victim.write_text("precious", encoding="utf-8")
    bus = events.JsonlEventBus(root)
    with pytest.raises(ValueError):
        bus.drain("../victim")
    assert victim.read_text(encoding="utf-8") == "precious"


def test_bus_still_usable_after_rejected_traversal(tmp_path):
    root = tmp_path / "root"
    bus = events.JsonlEventBus(root)
    with pytest.raises(ValueError):
        bus.publish(_event_with_plan("../etc/passwd"))
    # A legitimate publish must still succeed and write inside the root,
    # confirming the validator did not corrupt object state.
    ev = events.make_event("story_ready", "legit")
    bus.publish(ev)
    log = root / "legit.events.jsonl"
    assert log.exists()
    assert json.loads(log.read_text(encoding="utf-8").rstrip("\n")) == ev


def test_drain_still_usable_after_rejected_traversal(tmp_path):
    root = tmp_path / "root"
    bus = events.JsonlEventBus(root)
    with pytest.raises(ValueError):
        bus.drain("../etc/passwd")
    # A legitimate drain must still work on a real plan.
    ev = events.make_event("story_ready", "legit")
    bus.publish(ev)
    result = bus.drain("legit")
    assert result == [ev]