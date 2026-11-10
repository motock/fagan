"""Connect-retry tests for :mod:`pipeline.events_nats`.

The standalone pipeline container starts its scheduler before NATS can
listen, so the publisher has to ride out the broker's startup window instead
of killing the first reconcile tick. These tests inject a virtual clock, so
no case sleeps for real seconds.

Every case drives the public API only (:class:`NatsEventBus` plus the
``PIPELINE_EVENT_PUBLISH_CONNECT_RETRY_SECONDS`` environment variable) and
never reads the implementation's source.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from pathlib import Path

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_events_nats_config.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline.events_nats import EventPublishError, NatsEventBus

LOCAL_URL = "nats://127.0.0.1:4222"
ENV_RETRY_WINDOW = "PIPELINE_EVENT_PUBLISH_CONNECT_RETRY_SECONDS"

EVENT_TYPE = "agent_dispatched"
PLAN = "bus-plan"
PAYLOAD_MARKER = "payload-marker-do-not-log"

_NATS_LOGGER = "pipeline.events_nats"
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The real sleep, captured before any test patches ``asyncio.sleep``: the
#: fake broker's publish delay must stay a real wait.
_REAL_ASYNCIO_SLEEP = asyncio.sleep

_NATS_ENV_VARS = (
    "PIPELINE_NATS_URL",
    "PIPELINE_NATS_CREDS_FILE",
    "PIPELINE_NATS_TLS_CA_FILE",
    "PIPELINE_NATS_STREAM",
    "PIPELINE_EVENT_PUBLISH_TIMEOUT_S",
    "PIPELINE_NATS_MANAGE_STREAM",
    "PIPELINE_NATS_DURABLE_TYPES",
    ENV_RETRY_WINDOW,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeNotFound(Exception):
    """Stand-in for ``nats.js.errors.NotFoundError``."""


class _FakeJetStream:
    """Records what the bus asks JetStream to do; answers like a real server."""

    def __init__(self) -> None:
        self.publish_calls: list[dict] = []
        self.stream_info_calls: list[str] = []
        self.stream_missing = False
        self.publish_delay = 0.0
        self.publish_error: BaseException | None = None

    async def stream_info(self, name):
        self.stream_info_calls.append(name)
        if self.stream_missing:
            raise _FakeNotFound(f"stream {name} not found")
        return {"config": {"name": name}}

    async def add_stream(self, **kwargs):
        return {"config": kwargs}

    async def publish(self, subject, payload=b"", headers=None, **kwargs):
        self.publish_calls.append(
            {"subject": subject, "payload": payload, "headers": headers}
        )
        if self.publish_delay:
            await _REAL_ASYNCIO_SLEEP(self.publish_delay)
        if self.publish_error is not None:
            raise self.publish_error
        return types.SimpleNamespace(stream="PIPELINE", seq=len(self.publish_calls))


class _FakeNatsConnection:
    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js
        self.closed = False

    def jetstream(self, **kwargs):
        return self._js

    async def close(self):
        self.closed = True


class _FakeBroker:
    """The fake ``nats`` module's ``connect``: refuses on demand."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.refuse_first = 0
        self.refuse_all = False
        self.js = _FakeJetStream()

    async def connect(self, *args, **kwargs):
        self.connect_calls += 1
        if self.refuse_all or self.connect_calls <= self.refuse_first:
            raise OSError("connection refused")
        return _FakeNatsConnection(self.js)


class _VirtualClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._reads = 0

    def sleep(self, seconds) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(0.0, float(seconds))

    async def async_sleep(self, seconds) -> None:
        self.sleep(seconds)

    def monotonic(self) -> float:
        self._reads += 1
        if self._reads > 100:
            # Safety net: a retry loop that never sleeps would spin forever
            # against a clock that only a sleep moves.
            self.now += 1.0
        return self.now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_nats(monkeypatch):
    """Install a fake ``nats`` package before the bus imports it."""
    broker = _FakeBroker()

    module = types.ModuleType("nats")
    module.connect = broker.connect
    js_module = types.ModuleType("nats.js")
    errors_module = types.ModuleType("nats.js.errors")
    errors_module.NotFoundError = _FakeNotFound
    js_module.errors = errors_module
    module.js = js_module

    monkeypatch.setitem(sys.modules, "nats", module)
    monkeypatch.setitem(sys.modules, "nats.js", js_module)
    monkeypatch.setitem(sys.modules, "nats.js.errors", errors_module)
    return broker


@pytest.fixture
def clean_env(monkeypatch):
    """Start every case from "nothing NATS-related is set"."""
    for name in _NATS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def virtual_clock(monkeypatch):
    """Replace every clock a retry loop could use with a virtual one."""
    clock = _VirtualClock()
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "perf_counter", clock.monotonic)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0 + clock.now)
    monkeypatch.setattr(asyncio, "sleep", clock.async_sleep)
    return clock


@pytest.fixture
def make_bus(clean_env, fake_nats):
    """Build buses on the fake broker; every one is closed after the case."""
    built: list[NatsEventBus] = []

    def _make(**env) -> NatsEventBus:
        for name, value in env.items():
            clean_env.setenv(name, value)
        clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
        bus = NatsEventBus()
        built.append(bus)
        return bus

    try:
        yield _make
    finally:
        for bus in built:
            bus.close()


def _event(**overrides):
    event = {
        "type": EVENT_TYPE,
        "plan": PLAN,
        "story_key": "BR-03",
        "payload": {"note": PAYLOAD_MARKER},
    }
    event.update(overrides)
    return event


def _records(caplog, *levels: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _NATS_LOGGER and record.levelno in levels
    ]


def _attempts_with_window(make_bus, fake_nats, window: str | None, clock) -> int:
    """Publish to a broker that refuses everything; report the attempts.

    The virtual clock is reset first: the clock's own anti-spin safety net
    makes time advance faster once it has been read many times, so each
    measurement must start from an unread clock to describe one publish only.
    """
    clock.now = 0.0
    clock._reads = 0
    fake_nats.refuse_all = True
    env = {} if window is None else {ENV_RETRY_WINDOW: window}
    bus = make_bus(**env)
    baseline = fake_nats.connect_calls
    with pytest.raises(EventPublishError):
        bus.publish(_event())
    return fake_nats.connect_calls - baseline


# ---------------------------------------------------------------------------
# The retry itself
# ---------------------------------------------------------------------------
def test_a_broker_that_refuses_twice_is_still_reached(
    make_bus, fake_nats, virtual_clock, caplog
):
    """A broker that is slow to start must not lose the event."""
    fake_nats.refuse_first = 2
    delivered: list[dict] = []

    with caplog.at_level(logging.INFO, logger=_NATS_LOGGER):
        bus = make_bus(**{ENV_RETRY_WINDOW: "30"})
        bus.subscribe(EVENT_TYPE, delivered.append)
        bus.publish(_event())

    assert fake_nats.connect_calls == 3, (
        "the connection must be retried until the broker answers"
    )
    assert len(fake_nats.js.publish_calls) == 1, "the event must be published once"
    assert len(delivered) == 1, "the event must be delivered once"
    assert len(_records(caplog, logging.INFO)) == 1, (
        "exactly one INFO line reports the connection that had to be retried"
    )


def test_a_broker_that_refuses_for_the_whole_window_raises_the_unchanged_error(
    make_bus, fake_nats, virtual_clock, caplog
):
    """When the window is over, the publish fails the way it always did."""
    fake_nats.refuse_all = True

    with caplog.at_level(logging.DEBUG, logger=_NATS_LOGGER):
        bus = make_bus(**{ENV_RETRY_WINDOW: "30"})
        baseline = fake_nats.connect_calls
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event())

    message = str(excinfo.value)
    assert EVENT_TYPE in message
    assert "could not reach the NATS broker" in message, (
        "the exhausted window must report the unchanged error"
    )
    assert PAYLOAD_MARKER not in message

    failed_attempts = fake_nats.connect_calls - baseline
    assert failed_attempts > 1, "the window must actually be used"
    assert len(_records(caplog, logging.DEBUG, logging.WARNING)) >= min(
        failed_attempts, 2
    ), "every failed attempt must be visible in the log"
    assert not _records(caplog, logging.INFO), (
        "a connection that never succeeded must not be reported as recovered"
    )


def test_a_publish_timeout_is_not_retried_as_a_connection_failure(make_bus, fake_nats):
    """A publish that times out on a live connection is not republished."""
    bus = make_bus(PIPELINE_EVENT_PUBLISH_TIMEOUT_S="0.2")
    baseline = fake_nats.connect_calls
    fake_nats.js.publish_delay = 10.0

    with pytest.raises(EventPublishError) as excinfo:
        bus.publish(_event(event_id="e-1"))

    assert EVENT_TYPE in str(excinfo.value)
    assert "timed out" in str(excinfo.value)
    assert fake_nats.connect_calls == baseline, (
        "a publish timeout must not start a connection retry loop"
    )


def test_a_refused_publish_is_not_retried_as_a_connection_failure(make_bus, fake_nats):
    """A JetStream refusal is not a connection failure: no reconnect."""
    bus = make_bus()
    baseline = fake_nats.connect_calls
    fake_nats.js.publish_error = RuntimeError("jetstream refused the message")

    with pytest.raises(EventPublishError) as excinfo:
        bus.publish(_event(event_id="e-1"))

    assert EVENT_TYPE in str(excinfo.value)
    assert PAYLOAD_MARKER not in str(excinfo.value)
    assert fake_nats.connect_calls == baseline, (
        "a JetStream refusal must not start a connection retry loop"
    )


def test_a_missing_stream_is_not_retried_as_a_connection_failure(
    clean_env, fake_nats, virtual_clock
):
    """A stream JetStream does not have is a refusal, not a dead broker."""
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    fake_nats.js.stream_missing = True

    with pytest.raises(ValueError):
        NatsEventBus()

    assert fake_nats.connect_calls == 1, (
        "a JetStream refusal must not be retried as a connection failure"
    )


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def test_a_window_of_zero_makes_exactly_one_connection_attempt(
    make_bus, fake_nats, virtual_clock
):
    """``0`` disables the retry: the publish tries once and gives up."""
    fake_nats.refuse_all = True
    bus = make_bus(**{ENV_RETRY_WINDOW: "0"})
    baseline = fake_nats.connect_calls

    with pytest.raises(EventPublishError) as excinfo:
        bus.publish(_event())

    assert "could not reach the NATS broker" in str(excinfo.value)
    assert fake_nats.connect_calls - baseline == 1, (
        "a window of 0 must make exactly one connection attempt"
    )


@pytest.mark.parametrize("raw", ["not-a-number", "twelve", "-5", "-0.5"])
def test_an_unusable_window_degrades_to_the_default(
    make_bus, fake_nats, virtual_clock, caplog, raw
):
    """A malformed or negative window is reported and ignored, never fatal."""
    fake_nats.refuse_all = True

    with caplog.at_level(logging.WARNING, logger=_NATS_LOGGER):
        bus = make_bus(**{ENV_RETRY_WINDOW: raw})
        baseline = fake_nats.connect_calls
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event())

    assert EVENT_TYPE in str(excinfo.value)
    assert "could not reach the NATS broker" in str(excinfo.value)
    assert fake_nats.connect_calls - baseline == 1, (
        "an unusable window must fall back to the default of 0 (disabled), so "
        "the publish makes exactly one connection attempt"
    )
    assert _records(caplog, logging.WARNING), (
        "an unusable window must be reported with a warning"
    )


def test_an_unusable_window_behaves_exactly_like_the_default(
    make_bus, fake_nats, virtual_clock
):
    """The fallback is the default window, not some other value."""
    unusable = _attempts_with_window(make_bus, fake_nats, "not-a-number", virtual_clock)
    disabled = _attempts_with_window(make_bus, fake_nats, "0", virtual_clock)
    default = _attempts_with_window(make_bus, fake_nats, None, virtual_clock)

    assert unusable == disabled, (
        "an unusable window must fall back to the default window"
    )
    assert default == disabled, "the default window must be 0 (disabled)"


def test_the_env_unset_fails_fast_within_the_publish_timeout(clean_env):
    """Unset means no retry: ADR 0010's fail-fast bound still holds.

    One publish is bounded by its publish timeout, so a broker that is not
    listening must surface as ``EventPublishError`` well inside the contract's
    5 second budget -- a default retry window would stretch every caller's
    failed publish far past it.
    """
    pytest.importorskip("nats")
    clean_env.setenv("PIPELINE_NATS_URL", "nats://127.0.0.1:1")
    clean_env.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "0.2")

    bus = NatsEventBus()
    try:
        started = time.monotonic()
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event())
        elapsed = time.monotonic() - started
    finally:
        bus.close()

    assert "could not reach the NATS broker" in str(excinfo.value)
    assert elapsed < 5.0, (
        "an unset retry window must not stretch a failed publish past the "
        f"publish timeout (took {elapsed:.1f}s)"
    )


def test_the_window_is_read_at_call_time_and_not_cached(
    make_bus, clean_env, fake_nats, virtual_clock
):
    """A window set after the bus was built still governs the publish."""
    fake_nats.refuse_all = True
    bus = make_bus(**{ENV_RETRY_WINDOW: "0"})
    baseline = fake_nats.connect_calls

    clean_env.setenv(ENV_RETRY_WINDOW, "30")
    with pytest.raises(EventPublishError):
        bus.publish(_event())

    assert fake_nats.connect_calls - baseline > 1, (
        "the window must be read when the publish happens, not when the bus "
        "was built"
    )


def test_a_window_of_zero_set_after_construction_still_disables_the_retry(
    make_bus, clean_env, fake_nats, virtual_clock
):
    """The same, the other way round: a cached window cannot be used."""
    fake_nats.refuse_all = True
    bus = make_bus()
    baseline = fake_nats.connect_calls

    clean_env.setenv(ENV_RETRY_WINDOW, "0")
    with pytest.raises(EventPublishError):
        bus.publish(_event())

    assert fake_nats.connect_calls - baseline == 1, (
        "a window of 0 must make exactly one connection attempt"
    )


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------
def test_reference_documents_the_connect_retry_window():
    """REFERENCE.md's NATS table must carry the new variable."""
    lines = (_REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8").splitlines()

    anchor = next(
        (i for i, line in enumerate(lines) if "`PIPELINE_EVENT_PUBLISH_TIMEOUT_S`" in line),
        None,
    )
    assert anchor is not None, "the NATS environment table must still be there"

    row = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("| `PIPELINE_EVENT_PUBLISH_CONNECT_RETRY_SECONDS`")
        ),
        None,
    )
    assert row is not None, (
        "REFERENCE.md must document PIPELINE_EVENT_PUBLISH_CONNECT_RETRY_SECONDS"
    )
    assert row > anchor, (
        "the new variable belongs next to PIPELINE_EVENT_PUBLISH_TIMEOUT_S"
    )
    assert "| `0` |" in lines[row], (
        "the row must document the default of 0 (disabled)"
    )


def test_the_adr_explains_the_shared_network_namespace():
    """ADR 0010 must explain why the publisher waits for the broker."""
    text = (_REPO_ROOT / "docs/adr/0010-event-delivery-contract-and-nats-jetstream.md").read_text(
        encoding="utf-8"
    )
    paragraphs = [p.lower() for p in text.split("\n\n")]

    explained = [
        p
        for p in paragraphs
        if "network namespace" in p
        and ("broker" in p or "publisher" in p or "nats" in p)
    ]
    assert explained, (
        "ADR 0010 must explain that the shared network namespace forces the "
        "pipeline to start before the broker"
    )
