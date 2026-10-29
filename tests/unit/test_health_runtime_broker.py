"""TDD tests for EV-02: JetStream broker status in the health ``runtime`` block.

``/api/health`` already reports which event bus the process selected
(``runtime.event_bus``). When that selection is the NATS JetStream bus, an
operator also needs the delivery backlog: per durable consumer how many
messages are pending, how many were redelivered and how many were
dead-lettered. This module pins that surface:

* the ``inprocess`` default keeps reporting ``event_bus`` and gains **no**
  ``broker`` key (nothing to report when there is no broker);
* a NATS selection reports ``runtime.broker`` exactly as
  ``pipeline.events_nats_consumer.consumer_status()`` returns it;
* an unreachable broker reports ``{"reachable": False}`` and the health call
  still succeeds — a health probe must never 500 because a broker is down;
* the consumer-status cache holds its value for 30 s from the fetch (never
  sliding), and a failing collection is cached the same way without ever
  raising or echoing the failure text.

These tests are RED until the implementation lands.
"""

from __future__ import annotations

import asyncio
import inspect

import pipeline.server  # noqa: F401  (pre-existing import cycle: import before pipeline.*)
from app import dashboard
from pipeline import events_nats_consumer as consumer

#: The exact stub shape the NATS branch must pass through unchanged.
_LAG = {"pipeline-notification": {"pending": 3, "redelivered": 1, "dead_lettered": 0}}


def _run(value):
    """Resolve *value*: await it when it is awaitable, else return it as is.

    ``health()`` is a plain sync function today, but resolving both shapes keeps
    these assertions about the payload rather than about the calling convention.
    """
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _freeze_cache(monkeypatch, now: float) -> tuple[dict, list[float]]:
    """Pin the status cache to a fake clock; return the clock and a call log.

    ``_status_cache`` is replaced so a shared cache cannot leak between tests;
    ``time.monotonic`` is patched on the module's own ``time`` reference, which
    is why the implementation must call ``time.monotonic()`` and not
    ``time.time()`` (a wall-clock step would make a stale entry look fresh).
    """
    clock = {"now": now}
    calls: list[float] = []

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(consumer.time, "monotonic", fake_monotonic, raising=False)
    monkeypatch.setattr(
        consumer, "_status_cache", {"expires_at": None, "value": None}, raising=False
    )
    return clock, calls


def test_health_runtime_reports_inprocess_event_bus_by_default(monkeypatch):
    """The default bus is reported, and there is no broker block to report."""
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    body = _run(dashboard.health())

    assert "runtime" in body
    assert body["runtime"]["event_bus"] == "inprocess"
    assert "broker" not in body["runtime"]


def test_health_includes_broker_status_when_event_bus_is_nats(monkeypatch):
    """A NATS selection surfaces the per-durable lag exactly as collected."""
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    # Patched on the consumer module: health() imports the name lazily inside
    # its NATS branch, so patching the source module is what it will see.
    monkeypatch.setattr(consumer, "consumer_status", lambda: _LAG, raising=False)

    body = _run(dashboard.health())

    assert body["runtime"]["event_bus"] == "nats"
    assert body["runtime"]["broker"] == _LAG


def test_health_reports_unreachable_broker_without_failing(monkeypatch):
    """A broker that cannot be reached degrades the block, never the probe."""
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    unreachable = {"reachable": False}
    monkeypatch.setattr(consumer, "consumer_status", lambda: unreachable, raising=False)

    body = _run(dashboard.health())

    assert body["ok"] is True
    assert body["runtime"]["broker"] == {"reachable": False}


def test_consumer_status_caches_for_30s_without_sliding(monkeypatch):
    """The value is held for 30 s from the fetch; a hit does not extend it."""
    assert callable(getattr(consumer, "consumer_status", None))
    clock, calls = _freeze_cache(monkeypatch, now=100.0)

    def fake_collect() -> dict:
        calls.append(clock["now"])
        return _LAG

    monkeypatch.setattr(consumer, "_collect_consumer_status", fake_collect, raising=False)

    assert consumer.consumer_status() == _LAG  # t=100.0 -> fetch
    clock["now"] = 110.0
    assert consumer.consumer_status() == _LAG  # t=110.0 -> cached hit
    clock["now"] = 131.0
    assert consumer.consumer_status() == _LAG  # t=131.0 -> expired, fetch again

    assert calls == [100.0, 131.0]


def test_consumer_status_never_raises_and_caches_unreachable(monkeypatch):
    """A collection failure becomes the unreachable marker, cached, never raised."""
    assert callable(getattr(consumer, "consumer_status", None))
    clock, calls = _freeze_cache(monkeypatch, now=100.0)

    def exploding_collect() -> dict:
        calls.append(clock["now"])
        raise RuntimeError("secret-token-abc")

    monkeypatch.setattr(
        consumer, "_collect_consumer_status", exploding_collect, raising=False
    )

    first = consumer.consumer_status()
    clock["now"] = 110.0
    second = consumer.consumer_status()

    assert first == {"reachable": False}
    assert second == {"reachable": False}
    assert calls == [100.0]  # the failure is cached, not retried on every call
    assert "secret-token-abc" not in repr(first) + repr(second)

    clock["now"] = 131.0
    assert consumer.consumer_status() == {"reachable": False}
    assert calls == [100.0, 131.0]  # expiry retries the broker
