"""Tests for the ``notification`` bus wiring in ``pipeline.event_wiring``.

These tests are written FIRST (TDD). They verify the contract described in the
notification-bus-wiring story:

* ``build_bus()`` subscribes :func:`file_log_sink` to the ``notification``
  event type (in addition to the pre-existing ``wake_handler`` on
  ``agent_done``).
* A process-level singleton ``get_bus()`` returns the same bus object on every
  call and never re-subscribes.
* Publishing a ``notification`` event onto a freshly built bus drives the real
  dispatch path and lands a line in ``<plan>.notifications.log``.
* A sink that raises must not break ``publish`` (the tick keeps going) and the
  error must be logged.
* A ``notification`` published from inside another handler (e.g. an
  ``agent_done`` handler) is delivered, with no recursion or deadlock.

The implementation changes do not exist yet, so the assertions below that
reference ``file_log_sink`` / ``get_bus`` / the ``notification`` subscription
must fail -- that is the intended RED state for this dispatch.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import event_wiring
from pipeline.event_wiring import build_bus, get_bus, wake_handler
from pipeline.events import make_event
from pipeline.notification_sinks import file_log_sink

# ---------------------------------------------------------------------------
# Registration: build_bus subscribes the right handlers to the right topics
# ---------------------------------------------------------------------------

def test_build_bus_subscribes_file_log_sink_to_notification():
    """``build_bus`` must register ``file_log_sink`` on ``notification``.

    Assert on object identity, not on a name: the registered handler must *be*
    the very ``file_log_sink`` object imported from ``pipeline.notification_sinks``.
    """
    bus = build_bus()
    assert file_log_sink in bus._handlers["notification"]


def test_build_bus_still_subscribes_wake_handler():
    """Regression guard: the pre-existing ``agent_done`` wiring is untouched."""
    bus = build_bus()
    assert bus._handlers["agent_done"] == [wake_handler]


def test_build_bus_does_not_subscribe_file_log_sink_to_agent_done():
    """``file_log_sink`` must only be on ``notification``, not ``agent_done``."""
    bus = build_bus()
    assert file_log_sink not in bus._handlers.get("agent_done", [])


def test_build_bus_does_not_subscribe_wake_handler_to_notification():
    """``wake_handler`` must only be on ``agent_done``, not ``notification``."""
    bus = build_bus()
    assert wake_handler not in bus._handlers.get("notification", [])


# ---------------------------------------------------------------------------
# End-to-end dispatch: publishing a notification reaches the sink on disk
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_plan_dir(tmp_path, monkeypatch):
    """Patch ``PLAN_DIR`` everywhere the sink reads it.

    ``file_log_sink`` reads ``persistence.PLAN_DIR``; ``persistence`` imports
    ``PLAN_DIR`` from ``.paths`` at module load. Patch both the canonical
    location and the re-bound name so the sink writes into ``tmp_path``.
    """
    from pipeline import paths, persistence

    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def test_publishing_a_notification_reaches_the_sink(patched_plan_dir):
    """Publish a real ``notification`` event onto ``build_bus()`` and assert
    the free-text line landed in ``<plan>.notifications.log``.

    This exercises the real ``bus.publish`` dispatch path end to end -- not the
    sink in isolation.
    """
    bus = build_bus()
    plan = "p1"
    event = make_event(
        "notification",
        plan,
        payload={"message": "ci_pending_stalled", "event": "ci_pending_stalled"},
    )
    bus.publish(event)

    log_path = patched_plan_dir / f"{plan}.notifications.log"
    assert log_path.exists(), "notification log file was not created"
    line = log_path.read_text()
    assert "ci_pending_stalled" in line
    # The sink writes "<ts> <message>\n"; the timestamp from the event must be
    # present as the leading field.
    assert event["ts"] in line


# ---------------------------------------------------------------------------
# Singleton: get_bus returns the same object and never re-subscribes
# ---------------------------------------------------------------------------

def test_get_bus_is_a_singleton():
    """``get_bus`` must return the SAME object on every call within a process."""
    event_wiring._BUS = None
    first = get_bus()
    second = get_bus()
    assert first is second


def test_get_bus_does_not_duplicate_subscriptions():
    """A second/third call to ``get_bus`` must not leave duplicate handlers.

    Reset the singleton, call ``get_bus`` three times, and assert the
    ``notification`` handler list still has exactly one entry.
    """
    event_wiring._BUS = None
    get_bus()
    get_bus()
    bus = get_bus()
    assert bus._handlers["notification"].count(file_log_sink) == 1
    assert file_log_sink in bus._handlers["notification"]


def test_get_bus_returns_a_bus_with_wake_handler():
    """The singleton bus must still carry the ``agent_done`` wiring."""
    event_wiring._BUS = None
    bus = get_bus()
    assert bus._handlers["agent_done"] == [wake_handler]


def test_get_bus_module_level_name_is_BUS():
    """The resettable singleton must live at the module-level name ``_BUS``.

    Tests reset it by setting ``pipeline.event_wiring._BUS = None``; keep that
    name exactly.
    """
    assert hasattr(event_wiring, "_BUS")


# ---------------------------------------------------------------------------
# Robustness: a raising sink must never break the tick
# ---------------------------------------------------------------------------

def test_raising_sink_does_not_break_publish(caplog):
    """A sink that raises must not propagate; ``publish`` returns normally and
    the error is logged at ERROR level.

    This is the 'a sink failure must never break the tick' criterion,
    exercised at the bus level.
    """
    bus = build_bus()

    def boom(event):
        raise RuntimeError("sink exploded")

    bus.subscribe("notification", boom)
    event = make_event("notification", "p1", payload={"message": "x"})

    with caplog.at_level(logging.ERROR, logger="pipeline.events"):
        # Must not raise.
        result = bus.publish(event)
    assert result is None
    # The bus logs handler failures at ERROR with the handler's qualname.
    assert any(
        "boom" in record.getMessage() and record.levelno == logging.ERROR
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Cross-handler dispatch: a notification published from inside another handler
# ---------------------------------------------------------------------------

def test_notification_published_from_inside_another_handler_is_delivered():
    """An ``agent_done`` handler that itself publishes a ``notification`` onto
    the same bus must have that notification delivered, with no recursion or
    deadlock.

    The two handler lists (``agent_done`` and ``notification``) are distinct
    and neither is mutated during dispatch.
    """
    bus = build_bus()
    delivered = []

    def notifier(event):
        # Publish a notification from inside an agent_done handler. This must
        # dispatch the notification handlers (file_log_sink) synchronously and
        # return without recursing back into agent_done or deadlocking.
        bus.publish(make_event("notification", event["plan"], payload={"message": "from-handler"}))

    def capture(event):
        delivered.append(event["payload"].get("message"))

    # Replace the agent_done wiring with our notifier so wake_handler (which
    # needs a manifest) does not run for this test.
    bus._handlers["agent_done"] = [notifier]
    # Keep file_log_sink on notification but also add a capturing handler so we
    # can assert delivery without depending on disk state.
    bus.subscribe("notification", capture)

    bus.publish(make_event("agent_done", "p1", story_key="S1"))

    assert "from-handler" in delivered
    # The agent_done list must not have been mutated to include notification
    # handlers, and notification list must still be distinct.
    assert bus._handlers["agent_done"] == [notifier]
    assert capture in bus._handlers["notification"]
    assert notifier not in bus._handlers["notification"]