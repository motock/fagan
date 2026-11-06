"""Consumer liveness in ``consumer_status``: ``subscribed`` / ``oldest_pending_age_s``.

The dashboard's broker probe reports one entry per durable consumer. Besides
the backlog counters, an operator needs to know whether the consumer actually
has a client attached, and how old its oldest undelivered message is. The
durable consumers in this repo are *pull* consumers
(``pipeline/events_nats_consumer.py``: ``bus._js.pull_subscribe(...)``), so the
broker's ``ConsumerInfo.num_waiting`` — the count of pending fetch requests —
is the attached-client signal, not ``push_bound`` (which is always ``None``
for a pull consumer).

The age is an approximation: JetStream keeps no per-message timestamp for
undelivered messages, so ``ConsumerInfo.ack_floor.last_active`` is the newest
information the broker holds. It is reported only while messages are pending,
and it must survive a cache hit unchanged rather than being recomputed
against "now".
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

import nats
import pytest

from pipeline import events_nats
from pipeline import events_nats_consumer as consumer


class _FakeClock:
    """Deterministic wall + monotonic clock patched over the module's ``time``."""

    def __init__(self, wall: float, monotonic: float = 100.0) -> None:
        self.wall = wall
        self.mono = monotonic

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class _FakeJS:
    def __init__(self, info, calls: dict) -> None:
        self._info = info
        self._calls = calls

    async def consumer_info(self, stream, durable):
        self._calls["consumer_info"] += 1
        return self._info

    async def stream_info(self, stream, subjects_filter=None):
        return types.SimpleNamespace(state=types.SimpleNamespace(messages=0))


class _FakeNC:
    def __init__(self, info, calls: dict) -> None:
        self._info = info
        self._calls = calls

    def jetstream(self, *args, **kwargs):
        return _FakeJS(self._info, self._calls)

    async def close(self) -> None:
        self._calls["closed"] += 1


def _install_broker(monkeypatch, info, clock, *, fail: bool = False) -> dict:
    """Stand in for a broker holding one durable consumer named after ``agent_done``."""
    calls = {"consumer_info": 0, "closed": 0, "connect": 0}

    async def _connect(**kwargs):
        calls["connect"] += 1
        if fail:
            raise ConnectionError("no broker")
        return _FakeNC(info, calls)

    settings = types.SimpleNamespace(
        url="nats://127.0.0.1:4222",
        publish_timeout_s=0.5,
        creds_file=None,
        tls_ca_file=None,
        stream="PIPELINE",
        durable_types={"agent_done"},
    )

    monkeypatch.setattr(nats, "connect", _connect)
    monkeypatch.setattr(events_nats, "nats_settings_from_env", lambda: settings)
    monkeypatch.setattr(consumer, "time", clock)
    monkeypatch.setattr(
        consumer, "_status_cache", {"expires_at": None, "value": None}
    )
    return calls


def _info(**kwargs):
    """A ``ConsumerInfo``-shaped object; absent keys model an older client."""
    base = {"num_pending": 0, "num_redelivered": 0}
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _durable() -> str:
    return consumer._durable_name("agent_done")


def test_subscribed_true_when_a_client_is_waiting(monkeypatch):
    clock = _FakeClock(1_700_000_000.0)
    info = _info(num_waiting=2, num_pending=1)
    _install_broker(monkeypatch, info, clock)

    entry = consumer.consumer_status()[_durable()]

    assert entry["subscribed"] is True
    assert entry["pending"] == 1


def test_subscribed_false_when_no_client_is_waiting(monkeypatch):
    clock = _FakeClock(1_700_000_000.0)
    info = _info(num_waiting=0, num_pending=1)
    _install_broker(monkeypatch, info, clock)

    entry = consumer.consumer_status()[_durable()]

    assert entry["subscribed"] is False


def test_pending_zero_has_no_age_and_does_not_raise(monkeypatch):
    clock = _FakeClock(1_700_000_000.0)
    last_active = datetime.fromtimestamp(clock.wall - 12.7, tz=UTC)
    info = _info(
        num_waiting=1,
        num_pending=0,
        ack_floor=types.SimpleNamespace(last_active=last_active),
    )
    _install_broker(monkeypatch, info, clock)

    entry = consumer.consumer_status()[_durable()]

    assert entry["oldest_pending_age_s"] is None


def test_missing_liveness_fields_report_inactive_without_raising(monkeypatch):
    """An older client's info object may carry neither field; never fail the probe."""
    clock = _FakeClock(1_700_000_000.0)
    info = _info(num_pending=1)  # no num_waiting, no push_bound, no ack_floor
    _install_broker(monkeypatch, info, clock)

    entry = consumer.consumer_status()[_durable()]

    assert entry["subscribed"] is False
    assert entry["oldest_pending_age_s"] is None


def test_unreachable_broker_is_exactly_the_marker(monkeypatch):
    clock = _FakeClock(1_700_000_000.0)
    _install_broker(monkeypatch, _info(), clock, fail=True)

    assert consumer.consumer_status() == {"reachable": False}


def test_age_truncates_and_is_frozen_by_the_cache(monkeypatch):
    """12.7 s truncates to 12, and a cache hit returns the stored age untouched."""
    clock = _FakeClock(1_700_000_000.0)
    last_active = datetime.fromtimestamp(clock.wall - 12.7, tz=UTC)
    info = _info(
        num_waiting=1,
        num_pending=2,
        ack_floor=types.SimpleNamespace(last_active=last_active),
    )
    calls = _install_broker(monkeypatch, info, clock)

    assert consumer.consumer_status()[_durable()]["oldest_pending_age_s"] == 12

    clock.advance(20.0)
    entry = consumer.consumer_status()[_durable()]

    assert entry["oldest_pending_age_s"] == 12
    assert calls["consumer_info"] == 1


def test_future_timestamp_clamps_to_zero(monkeypatch):
    clock = _FakeClock(1_700_000_000.0)
    last_active = datetime.fromtimestamp(clock.wall + 5.0, tz=UTC)
    info = _info(
        num_pending=1,
        ack_floor=types.SimpleNamespace(last_active=last_active),
    )
    _install_broker(monkeypatch, info, clock)

    assert consumer.consumer_status()[_durable()]["oldest_pending_age_s"] == 0


def test_naive_timestamp_is_an_int_or_none(monkeypatch):
    """``last_active`` may parse without tzinfo; the value stays ``int | None``."""
    clock = _FakeClock(1_700_000_000.0)
    last_active = datetime(2020, 1, 1, 0, 0, 0) - timedelta(seconds=0)
    info = _info(
        num_pending=1,
        ack_floor=types.SimpleNamespace(last_active=last_active),
    )
    _install_broker(monkeypatch, info, clock)

    age = consumer.consumer_status()[_durable()]["oldest_pending_age_s"]

    assert isinstance(age, int) or age is None
    if age is not None:
        assert age >= 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
