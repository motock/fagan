"""Unit tests for the background retry that starts durable consumers late.

``start_consumers`` runs once, at scheduler-daemon boot. In the standalone
stack the ``nats`` container starts a few milliseconds after the ``pipeline``
container, so the first dial fails and -- before this feature -- nothing ever
tried again: both durable consumers stayed down until the container was
restarted. These cases pin the retry thread that closes that gap: it waits on
a stop event with 1 s, 2 s, 4 s ... up to 30 s backoff, logs nothing while the
broker stays down, and logs exactly one info line when the consumers start.

The fakes below stand in for ``nats-py`` and for the bus's loop thread; the
consumer only touches ``_ensure_loop``, ``_ensure_connection``,
``_durable_types``, ``_settings.stream``, ``_js.pull_subscribe``, ``fetch``,
``ack`` and ``nak``, so a recording fake is enough. Nothing is imported from
``tests/unit/test_events_nats_consumer.py``: these cases fail independently of
that file, which must keep passing unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline import events_nats_consumer as consumer
from pipeline.events import InProcessEventBus
from pipeline.events_nats import NatsEventBus

STREAM = "PIPELINE_UNIT"
RETRY_THREAD_NAME = "nats-consumer-start-retry"
LOGGER_NAME = "pipeline.events_nats_consumer"
INFO_TEXT = "durable consumers started after the broker became reachable"
WARNING_TEXT = (
    "durable consumers were not started: the NATS broker named by "
    "PIPELINE_NATS_URL is unreachable"
)
TYPE_WARNING = "the durable consumer %s was not started"


class _FakeSubscription:
    """A pull subscription with nothing to hand out."""

    async def fetch(self, batch: int = 1, timeout: float | None = None):
        await asyncio.sleep(0.01)
        raise TimeoutError


class _FakeJetStream:
    """Records ``pull_subscribe`` attempts; can fail chosen durables."""

    def __init__(self):
        self.attempts: list[str] = []
        self.calls: list[dict] = []
        self.fail_durables: dict[str, int] = {}
        self._lock = threading.Lock()

    async def pull_subscribe(self, subject, durable=None, stream=None, config=None):
        with self._lock:
            self.attempts.append(durable)
            remaining = self.fail_durables.get(durable, 0)
            if remaining:
                self.fail_durables[durable] = remaining - 1
                raise RuntimeError("the broker refused to create the consumer")
            subscription = _FakeSubscription()
            self.calls.append(
                {
                    "subject": subject,
                    "durable": durable,
                    "stream": stream,
                    "config": config,
                    "subscription": subscription,
                }
            )
        return subscription


class _FakeNatsBus(NatsEventBus):
    """A NATS bus whose broker answers only when told to.

    ``NatsEventBus.__init__`` is deliberately not called: the real one
    validates ``PIPELINE_NATS_*`` and dials a server. ``_ensure_loop`` is
    inherited unchanged, so the consumers run on a real loop thread exactly as
    they do in the daemon.
    """

    def __init__(self, durable_types=(), *, reachable=None, fail_connections=0):
        self._settings = SimpleNamespace(
            url="nats://127.0.0.1:1",
            stream=STREAM,
            publish_timeout_s=2.0,
            manage_stream=True,
            creds_file=None,
            tls_ca_file=None,
            durable_types=frozenset(durable_types),
        )
        self._handlers: dict[str, list] = {}
        self._durable_types = frozenset(durable_types)
        self._js = _FakeJetStream()
        self._nc = None
        self._stream_ready = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fake-nats-loop", daemon=True
        )
        self._thread.start()
        self._pid = os.getpid()
        #: When set, the broker answers; tests flip it to simulate a late start.
        self.reachable = reachable if reachable is not None else threading.Event()
        if reachable is None:
            self.reachable.set()
        #: How many further dials fail before the broker answers.
        self.fail_connections = fail_connections
        self.connections = 0
        self._dial_lock = threading.Lock()

    def _ensure_connection(self, loop):
        if not self.reachable.is_set():
            raise ConnectionError("the broker is not listening yet")
        with self._dial_lock:
            if self.fail_connections:
                self.fail_connections -= 1
                raise ConnectionError("the broker is not listening yet")
            self.connections += 1

    def close(self):
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_consumers_leak():
    """Every case starts and ends with no consumers and no retry thread."""
    consumer.stop_consumers()
    yield
    consumer.stop_consumers()


@pytest.fixture
def fast_retry(monkeypatch):
    """Keep the backoff short; the real curve is pinned by its own case."""
    monkeypatch.setattr(consumer, "_RETRY_INITIAL_S", 0.01, raising=False)
    monkeypatch.setattr(consumer, "_RETRY_MAX_S", 0.01, raising=False)


@pytest.fixture
def buses():
    built = []

    def build(**kwargs):
        bus = _FakeNatsBus(**kwargs)
        built.append(bus)
        return bus

    yield build
    # Stop the fetch loops while their loop still runs, then close the loop.
    consumer.stop_consumers()
    for bus in built:
        bus.close()


def _retry_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == RETRY_THREAD_NAME]


def _only_retry_thread() -> threading.Thread:
    threads = _retry_threads()
    assert len(threads) == 1, f"expected one {RETRY_THREAD_NAME} thread, saw {threads}"
    return threads[0]


def _retry_thread_handle() -> threading.Thread:
    """The module's retry-thread handle, which outlives the thread itself.

    ``threading.enumerate()`` cannot be used here: a thread that already
    finished is gone from it, and these cases finish fast. The module keeps its
    handle until ``stop_consumers`` clears it, so joining through the handle is
    race-free.
    """
    thread = getattr(consumer, "_retry_thread", None)
    if thread is None:
        threads = _retry_threads()
        assert len(threads) == 1, f"expected one {RETRY_THREAD_NAME} thread, saw {threads}"
        thread = threads[0]
    assert thread.name == RETRY_THREAD_NAME
    return thread


def _join_retry_thread(timeout: float = 5.0) -> threading.Thread:
    thread = _retry_thread_handle()
    thread.join(timeout)
    assert not thread.is_alive(), "the retry thread did not exit within the timeout"
    return thread


def _records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER_NAME]


class _RecordingEvent:
    """A stop event that records each wait instead of sleeping."""

    def __init__(self, fail_after: int = 20):
        self.waits: list[float] = []
        self._set = False
        self._fail_after = fail_after

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def clear(self) -> None:
        self._set = False

    def wait(self, delay: float | None = None) -> bool:
        self.waits.append(delay)
        if len(self.waits) > self._fail_after:
            raise RuntimeError("the retry loop did not stop after the broker answered")
        return False


def test_unreachable_broker_then_reachable_starts_every_durable_type(buses, fast_retry):
    """A late broker still gets both durable consumers, without a new call."""
    assert hasattr(consumer, "_RETRY_INITIAL_S")
    assert hasattr(consumer, "_RETRY_MAX_S")
    bus = buses(durable_types=("agent_done", "notification"), reachable=threading.Event())

    consumer.start_consumers(bus)
    assert consumer.running_consumer_names() == ()

    bus.reachable.set()
    _join_retry_thread()

    assert set(consumer.running_consumer_names()) == {
        "pipeline-agent_done",
        "pipeline-notification",
    }


def test_one_warning_then_one_info_however_many_attempts_fail(buses, fast_retry, caplog):
    """A broker that stays down must not flood the log; success logs once."""
    assert hasattr(consumer, "_retry_thread_lock") or hasattr(consumer, "_retry_stop")
    bus = buses(durable_types=("agent_done",), fail_connections=6)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        consumer.start_consumers(bus)
        _join_retry_thread()

    records = _records(caplog)
    warnings = [record for record in records if record.levelno == logging.WARNING]
    infos = [record for record in records if record.levelno == logging.INFO]

    assert [record.getMessage() for record in warnings] == [WARNING_TEXT]
    assert [record.getMessage() for record in infos] == [INFO_TEXT]
    assert infos[0].attempts == 6
    assert len(records) == 2
    assert consumer.running_consumer_names() == ("pipeline-agent_done",)


def test_reachable_first_call_starts_no_retry_thread(buses, fast_retry, caplog):
    """The happy path keeps today's single-shot start: no thread, no record."""
    assert hasattr(consumer, "_RETRY_INITIAL_S")
    assert hasattr(consumer, "_RETRY_MAX_S")
    assert hasattr(consumer, "_retry_stop")
    bus = buses(durable_types=("agent_done",))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        consumer.start_consumers(bus)

    assert consumer.running_consumer_names() == ("pipeline-agent_done",)
    assert _retry_threads() == []
    assert _records(caplog) == []


def test_second_call_while_unreachable_leaves_one_retry_thread(buses, fast_retry):
    """The module-level guard keeps a second start from spawning a duplicate."""
    assert hasattr(consumer, "_retry_stop")
    bus = buses(durable_types=("agent_done",), reachable=threading.Event())

    consumer.start_consumers(bus)
    consumer.start_consumers(bus)

    assert len(_retry_threads()) == 1
    assert consumer.running_consumer_names() == ()


def test_stop_while_unreachable_ends_the_retry_thread(buses, fast_retry):
    """A stop interrupts the wait at once and nothing starts afterwards."""
    assert hasattr(consumer, "_retry_stop")
    bus = buses(durable_types=("agent_done",), reachable=threading.Event())
    consumer.start_consumers(bus)
    thread = _only_retry_thread()

    started = time.monotonic()
    consumer.stop_consumers()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    thread.join(2.0)
    assert not thread.is_alive()

    # A broker that answers after the stop must not start anything: the thread
    # that would have retried is gone and no new start call was made.
    bus.reachable.set()
    threading.Event().wait(0.1)
    assert not thread.is_alive()
    assert consumer.running_consumer_names() == ()
    assert _retry_threads() == []


def test_non_nats_bus_starts_no_thread_and_no_consumer():
    """Only the scheduler daemon's NATS bus gets durable consumption."""
    assert hasattr(consumer, "_RETRY_INITIAL_S")
    bus = InProcessEventBus()

    consumer.start_consumers(bus)

    assert _retry_threads() == []
    assert consumer.running_consumer_names() == ()


def test_failed_type_is_retried_until_it_starts(buses, fast_retry, caplog):
    """A type that fails after a good connect is retried without the rest."""
    assert hasattr(consumer, "_RETRY_INITIAL_S")
    bus = buses(durable_types=("agent_done", "notification"))
    bus._js.fail_durables = {"pipeline-agent_done": 1}

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        consumer.start_consumers(bus)
        _join_retry_thread()

    assert set(consumer.running_consumer_names()) == {
        "pipeline-agent_done",
        "pipeline-notification",
    }
    # The healthy type was started once, first; only the failing one was tried
    # again. A restart of the healthy type would show up here.
    assert bus._js.attempts == [
        "pipeline-agent_done",  # failed on the first call
        "pipeline-notification",
        "pipeline-agent_done",  # retried, and only this one, once the broker answered
    ]

    records = _records(caplog)
    warnings = [record.getMessage() for record in records if record.levelno == logging.WARNING]
    infos = [record for record in records if record.levelno == logging.INFO]
    assert warnings == [TYPE_WARNING % "pipeline-agent_done"]
    assert [record.getMessage() for record in infos] == [INFO_TEXT]
    assert infos[0].attempts == 1


def test_wait_sequence_doubles_to_the_ceiling_with_the_real_constants(buses, monkeypatch):
    """The real constants give 1, 2, 4, 8, 16, 30, 30 and never exceed 30."""
    assert hasattr(consumer, "_RETRY_INITIAL_S")
    assert hasattr(consumer, "_RETRY_MAX_S")
    assert consumer._RETRY_INITIAL_S == 1.0
    assert consumer._RETRY_MAX_S == 30.0
    recorder = _RecordingEvent()
    monkeypatch.setattr(consumer, "_retry_stop", recorder)
    bus = buses(durable_types=("agent_done",), fail_connections=7)

    consumer.start_consumers(bus)
    _join_retry_thread()

    assert recorder.waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert max(recorder.waits) <= consumer._RETRY_MAX_S
    assert consumer.running_consumer_names() == ("pipeline-agent_done",)
