"""``dead_lettered`` in ``consumer_status`` counts dead-letter subjects only.

``consumer_status()`` reports, per durable consumer, how many messages are
retained on that consumer's dead-letter subject
(``pipeline.deadletter.<type>.>``).

The trap: JetStream's ``stream_info(stream, subjects_filter=...)`` does *not*
narrow ``state.messages`` — that stays the whole stream's total, and the filter
only narrows the per-subject breakdown in ``state.subjects``. Measured on the
live broker on 2026-11-06: stream ``PIPELINE`` held 74029 messages while both
durables had zero real dead letters, and the dashboard reported 73915 for both.

So the count has to be the sum of ``state.subjects``, which is what these tests
pin. The fake broker below mirrors the one in
``tests/unit/test_consumer_status_liveness.py`` and deliberately models a
``state`` object with no ``subjects`` attribute at all, because that is what an
older/other client hands back.
"""

from __future__ import annotations

import types

import nats
import pytest

from pipeline import events_nats
from pipeline import events_nats_consumer as consumer

_MISSING = object()


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


def _state(messages: int, subjects) -> types.SimpleNamespace:
    """A ``StreamState``-shaped object; ``_MISSING`` models an absent attribute."""
    if subjects is _MISSING:
        return types.SimpleNamespace(messages=messages)
    return types.SimpleNamespace(messages=messages, subjects=subjects)


class _FakeJS:
    def __init__(self, info, states: dict) -> None:
        self._info = info
        self._states = states

    async def consumer_info(self, stream, durable):
        return self._info

    async def stream_info(self, stream, subjects_filter=None):
        # subjects_filter == "pipeline.deadletter.<type>.>" -> the event type.
        event_type = (subjects_filter or "").split(".")[2]
        return types.SimpleNamespace(state=self._states[event_type])


class _FakeNC:
    def __init__(self, info, states: dict) -> None:
        self._info = info
        self._states = states

    def jetstream(self, *args, **kwargs):
        return _FakeJS(self._info, self._states)

    async def close(self) -> None:
        return None


def _install_broker(
    monkeypatch, *, states: dict, clock, durable_types, pending=0
) -> None:
    """Stand in for a broker holding one durable consumer per event type."""

    async def _connect(**kwargs):
        return _FakeNC(
            types.SimpleNamespace(num_pending=pending, num_redelivered=0), states
        )

    settings = types.SimpleNamespace(
        url="nats://127.0.0.1:4222",
        publish_timeout_s=0.5,
        creds_file=None,
        tls_ca_file=None,
        stream="PIPELINE",
        durable_types=set(durable_types),
    )

    monkeypatch.setattr(nats, "connect", _connect)
    monkeypatch.setattr(events_nats, "nats_settings_from_env", lambda: settings)
    monkeypatch.setattr(consumer, "time", clock)
    monkeypatch.setattr(consumer, "_status_cache", {"expires_at": None, "value": None})


@pytest.mark.parametrize(
    ("messages", "subjects", "expected"),
    [
        # The reproducing case: a non-empty stream total with no dead letters.
        pytest.param(50, {}, 0, id="empty-subjects-gives-zero-not-stream-total"),
        pytest.param(50, {"a": 2, "b": 3}, 5, id="sums-the-subject-map"),
        # The liveness fake's shape: no ``subjects`` attribute at all.
        pytest.param(0, _MISSING, 0, id="subjects-attribute-missing-gives-zero"),
        pytest.param(50, None, 0, id="subjects-none-gives-zero"),
        pytest.param(50, {"a": 2, "b": "3"}, 2, id="non-int-values-are-ignored"),
        # The live incident of 2026-11-06: 74029 in the stream, 0 dead letters.
        pytest.param(74029, {}, 0, id="live-incident-large-stream-no-dead-letters"),
    ],
)
def test_dead_lettered_counts_the_subject_map_only(
    monkeypatch, messages, subjects, expected
) -> None:
    clock = _FakeClock(1_700_000_000.0)
    _install_broker(
        monkeypatch,
        states={"agent_done": _state(messages, subjects)},
        clock=clock,
        durable_types={"agent_done"},
    )

    entry = consumer.consumer_status()[consumer._durable_name("agent_done")]

    assert entry["dead_lettered"] == expected


def test_each_durable_counts_only_its_own_dead_letter_subject(monkeypatch) -> None:
    clock = _FakeClock(1_700_000_000.0)
    _install_broker(
        monkeypatch,
        states={
            "notification": _state(74029, {"pipeline.deadletter.notification.x": 0}),
            "agent_done": _state(74029, {"pipeline.deadletter.agent_done.y": 4}),
        },
        clock=clock,
        durable_types={"notification", "agent_done"},
    )

    status = consumer.consumer_status()

    assert status[consumer._durable_name("notification")]["dead_lettered"] == 0
    assert status[consumer._durable_name("agent_done")]["dead_lettered"] == 4


def test_cached_dead_letter_count_survives_the_ttl_window(monkeypatch) -> None:
    """The 30 s cache holds the *count* across calls until it expires."""
    clock = _FakeClock(1_700_000_000.0)
    durable = consumer._durable_name("agent_done")
    states = {"agent_done": _state(50, {"a": 2, "b": 3})}
    _install_broker(
        monkeypatch,
        states=states,
        clock=clock,
        durable_types={"agent_done"},
    )

    assert consumer.consumer_status()[durable]["dead_lettered"] == 5

    # Within the TTL a changed broker answer must not leak through.
    states["agent_done"] = _state(50, {"a": 9})
    clock.advance(10.0)
    assert consumer.consumer_status()[durable]["dead_lettered"] == 5

    # Past the TTL the same changed answer is collected afresh.
    clock.advance(21.0)
    assert consumer.consumer_status()[durable]["dead_lettered"] == 9
