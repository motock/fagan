"""Broker-backed integration tests for :mod:`pipeline.events_nats_consumer`.

The durable consumer against a real ``nats-server -js``: what the scheduler
daemon runs when ``PIPELINE_EVENT_BUS=nats``. The skip/require rule, the server
fixture and the event envelope are the same ones BR-03's
``tests/integration/test_nats_bus.py`` established (that module's fixtures are
module-local, so the setup is repeated here rather than imported).

What only a live JetStream can answer:

* a handler that raises is redelivered with the same event, then acked;
* a handler that keeps raising is dead-lettered exactly once at the delivery
  limit -- one message on ``pipeline.deadletter.<type>.<plan>``, one
  ``event_dead_lettered`` sidecar record, one WARNING, then an ack;
* a consumer that stops while a message is still unacked leaves it on the
  durable consumer, so a restarted consumer picks it up with ``num_delivered``
  2 and loses nothing;
* a duplicate ``agent_done`` reaches :func:`pipeline.event_wiring.wake_handler`
  twice but dispatches once.

Each case uses its own durable event type, so the consumer names
(``pipeline-<type>``) and the filtered subjects never overlap and no case can
make another pass or fail by ordering. The session's stream is shared: a second
stream over ``pipeline.>`` would be rejected by the server.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline import event_wiring, events_nats_consumer, persistence
from pipeline.event_wiring import wake_handler
from pipeline.events_nats import NatsEventBus

NATS_SERVER = shutil.which("nats-server")
#: Whether a missing binary must fail instead of skip. ``tests/conftest.py``
#: deletes every ``PIPELINE_*`` variable before any test module is imported
#: (hermetic-env scrub), so the CI flag is read here as well as the ambient
#: ``GITHUB_ACTIONS`` marker that the same scrub leaves alone. CI sets both.
REQUIRE_NATS = (
    os.environ.get("PIPELINE_REQUIRE_NATS_TESTS") == "1"
    or os.environ.get("GITHUB_ACTIONS") == "true"
)

pytestmark = [
    pytest.mark.durable_bus,
    pytest.mark.skipif(
        NATS_SERVER is None and not REQUIRE_NATS,
        reason="nats-server not installed",
    ),
]

STREAM = "PIPELINE_IT"
PLAN = "nats-consumer-plan"


# -- server fixture (same setup as tests/integration/test_nats_bus.py) -------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"nats-server did not listen on 127.0.0.1:{port}")


async def _jetstream_ready(url: str, timeout: float = 20.0) -> None:
    """Wait until the server answers JetStream API calls."""
    import nats

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nc = None
        try:
            nc = await nats.connect(servers=[url])
            await nc.jetstream().account_info()
            return
        except Exception:  # noqa: BLE001 - any client error means "not ready yet"
            await asyncio.sleep(0.1)
        finally:
            if nc is not None:
                await nc.close()
    raise RuntimeError("nats-server JetStream API did not become available")


@pytest.fixture(scope="session")
def nats_url():
    """URL of a session-scoped ``nats-server -js``, or a loud failure in CI."""
    if NATS_SERVER is None:
        pytest.fail(
            "PIPELINE_REQUIRE_NATS_TESTS=1 but no nats-server binary is on PATH: "
            "the broker suite must not be skipped in CI"
        )

    port = _free_port()
    store = tempfile.mkdtemp(prefix="pipeline-nats-consumer-js-")
    process = subprocess.Popen(
        [NATS_SERVER, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", store],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"nats://127.0.0.1:{port}"
    try:
        try:
            _wait_for_port(port)
            asyncio.run(_jetstream_ready(url))
        except (RuntimeError, OSError):
            process.terminate()
            output = process.stdout.read() if process.stdout is not None else b""
            pytest.fail(f"nats-server failed to start: {output[-400:]!r}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
            process.kill()


@pytest.fixture
def nats_env(monkeypatch, nats_url):
    """Point the bus at the test server, with no durable type selected yet."""
    for name in (
        "PIPELINE_NATS_CREDS_FILE",
        "PIPELINE_NATS_TLS_CA_FILE",
        "PIPELINE_NATS_DURABLE_TYPES",
        "PIPELINE_EVENT_MAX_DELIVER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_NATS_URL", nats_url)
    monkeypatch.setenv("PIPELINE_NATS_STREAM", STREAM)
    monkeypatch.setenv("PIPELINE_NATS_MANAGE_STREAM", "1")
    monkeypatch.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "5")
    return {"url": nats_url, "stream": STREAM}


@pytest.fixture(autouse=True)
def _no_consumers_leak():
    """No consumer from one case may stay registered into the next."""
    events_nats_consumer.stop_consumers()
    yield
    events_nats_consumer.stop_consumers()


@pytest.fixture
def buses(nats_env, monkeypatch, tmp_path):
    """A consuming bus and a publishing bus, plus the plan directory.

    Two buses because a bus dispatches its own publish to the handlers it has
    recorded: publishing through the consuming bus would run a handler before
    the durable consumer ever saw the message, which is not how another
    process's event arrives.
    """
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    consumer_bus = NatsEventBus()
    publisher_bus = NatsEventBus()
    try:
        yield consumer_bus, publisher_bus, tmp_path
    finally:
        consumer_bus.close()
        publisher_bus.close()


# -- helpers -----------------------------------------------------------------
def _wait_for(predicate, *, timeout: float, what: str) -> None:
    """Poll *predicate* until true; fail the case with *what* when it is not."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _consumer_info(url: str, durable: str):
    """The server-side info for one durable consumer of the test stream."""
    import nats

    async def _info():
        nc = await nats.connect(servers=[url])
        try:
            return await nc.jetstream().consumer_info(STREAM, durable)
        finally:
            await nc.close()

    return asyncio.run(_info())


def _dead_letter_records(plan: str) -> list[dict]:
    """The ``event_dead_lettered`` sidecar records for *plan*."""
    path = persistence._notifications_jsonl_path(plan)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "event_dead_lettered":
            records.append(record)
    return records


def _watch_dead_letter_subjects(url: str, body, *, timeout: float):
    """Run *body* while recording every subject published under ``deadletter``.

    The watcher is a plain core-NATS subscription, so it sees each dead-letter
    message exactly as the broker routed it, independent of the stream.
    """

    async def _run():
        import nats

        nc = await nats.connect(servers=[url])
        seen: list[str] = []

        async def _record(message):
            seen.append(message.subject)

        await nc.subscribe("pipeline.deadletter.>", cb=_record)
        await nc.flush()
        try:
            await asyncio.wait_for(asyncio.to_thread(body, seen), timeout=timeout)
        finally:
            await nc.close()
        return seen

    return asyncio.run(_run())


def _event(event_type: str, **overrides):
    event = {
        "type": event_type,
        "plan": PLAN,
        "story_key": "BR-04",
        "payload": {"note": "consumer integration"},
    }
    event.update(overrides)
    return event


def _start_consumer_for(monkeypatch, event_type: str, bus) -> None:
    """Select *event_type* as the only durable type and start its consumer."""
    monkeypatch.setenv("PIPELINE_NATS_DURABLE_TYPES", event_type)
    bus._durable_types = frozenset({event_type})
    events_nats_consumer.start_consumers(bus)


# -- redelivery --------------------------------------------------------------
def test_a_raising_handler_is_redelivered_and_then_acked(
    buses, nats_env, monkeypatch
):
    """One failed attempt is retried with the same event, never dead-lettered."""
    consumer_bus, publisher_bus, _ = buses
    event_type = "notification"
    calls: list[dict] = []

    def handler(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails on purpose")

    consumer_bus.subscribe(event_type, handler)
    _start_consumer_for(monkeypatch, event_type, consumer_bus)

    publisher_bus.publish(_event(event_type, event_id="e-redeliver-1"))
    _wait_for(
        lambda: len(calls) >= 2,
        timeout=20.0,
        what="the redelivery after the raising handler",
    )

    assert len(calls) == 2, (
        f"one raising attempt must be redelivered exactly once, ran {len(calls)} times"
    )
    assert {call["event_id"] for call in calls} == {"e-redeliver-1"}, (
        "the redelivery must carry the same event"
    )
    assert _dead_letter_records(PLAN) == [], (
        "a delivery that succeeds on retry is never dead-lettered"
    )

    _wait_for(
        lambda: _consumer_info(
            nats_env["url"], f"pipeline-{event_type}"
        ).num_ack_pending
        == 0,
        timeout=10.0,
        what="the redelivered message to be acked",
    )


# -- dead-lettering ----------------------------------------------------------
def test_an_always_raising_handler_is_dead_lettered_exactly_once(
    buses, nats_env, monkeypatch, caplog
):
    """At the delivery limit: one dead-letter message, one record, one ack."""
    consumer_bus, publisher_bus, _ = buses
    event_type = "story_ready"
    attempts: list[float] = []

    def always_raising(event):
        attempts.append(time.monotonic())
        raise RuntimeError("always fails on purpose")

    consumer_bus.subscribe(event_type, always_raising)
    _start_consumer_for(monkeypatch, event_type, consumer_bus)

    observed: dict[str, list[str]] = {}

    def scenario(seen: list[str]) -> None:
        caplog.clear()
        publisher_bus.publish(_event(event_type, event_id="e-dead-1"))
        _wait_for(
            lambda: _dead_letter_records(PLAN),
            timeout=45.0,
            what="the dead-letter record after five failed deliveries",
        )
        observed["dead_letters"] = list(seen)

    _watch_dead_letter_subjects(nats_env["url"], scenario, timeout=60.0)

    assert len(attempts) == 5, (
        f"the delivery limit is 5 attempts, the handler ran {len(attempts)} times"
    )
    intervals = [
        later - earlier for earlier, later in itertools.pairwise(attempts)
    ]
    assert intervals[0] >= 0.7, (
        f"the first retry waits about a second, waited {intervals[0]:.2f}s"
    )
    # Each retry waits longer than the one before it. The exact curve (the nak
    # delay the consumer asks for is one second doubling per attempt, and the
    # consumer's own backoff schedule covers a crashed process) is pinned by
    # the unit tests; against a live broker the guarantee is that the wait
    # keeps growing instead of hammering the handler.
    assert all(
        later >= earlier for earlier, later in itertools.pairwise(intervals)
    ), f"retries must back off, waited {intervals!r}"
    assert intervals[-1] >= 2 * intervals[0], (
        f"the last retry must wait clearly longer than the first, got {intervals!r}"
    )

    dead_letters = observed.get("dead_letters", [])
    assert dead_letters == [f"pipeline.deadletter.{event_type}.{PLAN}"], (
        f"exactly one dead-letter message for the plan, got {dead_letters!r}"
    )

    records = _dead_letter_records(PLAN)
    assert len(records) == 1, f"exactly one dead-letter record, got {len(records)}"
    assert records[0]["dedup_key"] == "e-dead-1"
    assert records[0]["attempt"] == 5

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and record.name == events_nats_consumer.__name__
    ]
    assert any(
        event_type in message and "always_raising" in message for message in warnings
    ), (
        "the dead-letter warning must name the event type and the handler, got "
        f"{warnings!r}"
    )
    assert not any(
        "always fails on purpose" in message or "e-dead-1" in message
        for message in warnings
    ), "the dead-letter warning must never carry the exception text or the payload"

    _wait_for(
        lambda: _consumer_info(
            nats_env["url"], f"pipeline-{event_type}"
        ).num_ack_pending
        == 0,
        timeout=10.0,
        what="the dead-lettered message to be acked",
    )

    # Dead-lettering is terminal for this event but leaves the consumer usable:
    # a new event of the same type is still delivered.
    publisher_bus.publish(_event(event_type, event_id="e-dead-2"))
    _wait_for(
        lambda: len(attempts) == 6,
        timeout=20.0,
        what="a new event of the same type to be delivered",
    )


# -- restart resume ----------------------------------------------------------
def test_a_restarted_consumer_resumes_an_unacked_message(buses, nats_env, monkeypatch):
    """A message left unacked survives the consumer stopping; nothing is lost."""
    consumer_bus, publisher_bus, _ = buses
    event_type = "tests_passed"
    # A long retry delay keeps the message unacked while the consumer stops,
    # so the restart -- not a race with the redelivery -- is what resumes it.
    monkeypatch.setattr(events_nats_consumer, "_BACKOFF_BASE_S", 3.0)
    calls: list[dict] = []
    release = threading.Event()

    def handler(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails on purpose")
        # Hold the second delivery unacked so the broker's own view of it can
        # be read: it is the redelivery, and it is the same message.
        release.wait(timeout=20.0)

    consumer_bus.subscribe(event_type, handler)
    _start_consumer_for(monkeypatch, event_type, consumer_bus)

    publisher_bus.publish(_event(event_type, event_id="e-resume-1"))
    _wait_for(
        lambda: len(calls) == 1,
        timeout=20.0,
        what="the first delivery",
    )
    events_nats_consumer.stop_consumers()

    info = _consumer_info(nats_env["url"], f"pipeline-{event_type}")
    assert info.num_ack_pending == 1, (
        "a consumer that stops before acking must leave the message pending, "
        f"ack_pending={info.num_ack_pending}"
    )

    _start_consumer_for(monkeypatch, event_type, consumer_bus)
    _wait_for(
        lambda: len(calls) == 2,
        timeout=30.0,
        what="the restarted consumer to redeliver the pending message",
    )

    info = _consumer_info(nats_env["url"], f"pipeline-{event_type}")
    assert info.num_redelivered == 1, (
        "the message was delivered once before the stop and once after it, "
        f"got {info.num_redelivered} redeliveries"
    )
    assert info.num_ack_pending == 1, "the redelivery is still unacked"
    assert {call["event_id"] for call in calls} == {"e-resume-1"}, (
        "the restart must resume the same message, not a new one"
    )

    release.set()
    _wait_for(
        lambda: _consumer_info(
            nats_env["url"], f"pipeline-{event_type}"
        ).num_ack_pending
        == 0,
        timeout=15.0,
        what="the resumed message to be acked",
    )

    # Nothing is left, so a third consumer has nothing to deliver.
    events_nats_consumer.stop_consumers()
    _start_consumer_for(monkeypatch, event_type, consumer_bus)
    time.sleep(2.0)
    assert len(calls) == 2, (
        f"an acked message must not be delivered again, ran {len(calls)} times"
    )


# -- duplicate agent_done ----------------------------------------------------
def test_a_duplicate_agent_done_dispatches_once(buses, nats_env, monkeypatch, tmp_path):
    """A repeated ``agent_done`` is refused by the guard: one dispatch, not two."""
    consumer_bus, publisher_bus, _plan_dir = buses
    event_type = "agent_done"
    manifest_path = tmp_path / f"{PLAN}.manifest.json"
    manifest = {
        "plan": PLAN,
        "stories": {"BR-04": {"status": "in_progress", "phase": 1}},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dispatched: list[str] = []

    def fake_advance_pipeline(plan_name: str) -> dict:
        dispatched.append(plan_name)
        # Model the transition the real advance makes when it starts the next
        # phase: the story leaves "in_progress" until phase 2 begins.
        manifest["stories"]["BR-04"] = {"status": "tests_passed", "phase": 1}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {"ok": True, "dispatched": plan_name}

    monkeypatch.setattr(
        "pipeline.server.advance_pipeline", fake_advance_pipeline, raising=True
    )
    consumer_bus.subscribe(event_type, wake_handler)
    _start_consumer_for(monkeypatch, event_type, consumer_bus)

    # No event_id on either publish: with one, the stream would suppress the
    # second message by its Nats-Msg-Id, and the guard -- not the broker --
    # is what has to keep the duplicate harmless.
    duplicate = _event(event_type)
    publisher_bus.publish(dict(duplicate))
    _wait_for(
        lambda: len(dispatched) == 1,
        timeout=20.0,
        what="the first agent_done to dispatch",
    )

    publisher_bus.publish(dict(duplicate))
    _wait_for(
        lambda: _consumer_info(
            nats_env["url"], f"pipeline-{event_type}"
        ).num_ack_pending
        == 0
        and _consumer_info(nats_env["url"], f"pipeline-{event_type}").num_pending == 0,
        timeout=20.0,
        what="both agent_done messages to be acked",
    )

    assert len(dispatched) == 1, (
        "a duplicate agent_done must not dispatch a second time, dispatched "
        f"{len(dispatched)} times"
    )
    info = _consumer_info(nats_env["url"], f"pipeline-{event_type}")
    assert info.num_redelivered == 0, (
        "a refused agent_done is a normal return, so it is acked, not redelivered"
    )

    # The next phase's event must still reach the pipeline: the duplicate left
    # no high-water mark that swallows it.
    manifest["stories"]["BR-04"] = {"status": "in_progress", "phase": 2}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    publisher_bus.publish(_event(event_type, phase=2))
    _wait_for(
        lambda: len(dispatched) == 2,
        timeout=20.0,
        what="the phase-2 agent_done to dispatch",
    )
    assert dispatched == [PLAN, PLAN]
