"""Contract suite for ``EventBus`` in ``pipeline/events.py``.

Every event bus implementation must satisfy these behaviours unchanged. The
suite is split into two tiers:

* the **base tier** -- parametrized by ``bus_factory`` over every
  implementation in the tree (``InProcessEventBus`` and ``JsonlEventBus``) --
  pins the rules every bus owes its publishers and subscribers: an event
  reaches subscribers of its own type only, an unknown type never becomes an
  event, ``delivers()`` reports subscription state without changing it, an
  event dict survives a round trip with every key and value intact, and a
  raising handler does not silence the handlers registered after it.
* the **durable tier** -- marked ``durable_bus`` and parametrized over
  ``DURABLE_BUS_FACTORIES``, which lists only broker-backed implementations --
  pins ADR 0010 rules 1-4 and 6 on top of the base tier: redelivery after a
  raising handler, dead-lettering after ``PIPELINE_EVENT_MAX_DELIVER``
  attempts, publish-time duplicate suppression, and a bounded ``publish`` that
  raises ``EventPublishError`` when the broker cannot be reached.

Registering an implementation means appending a factory to ``BUS_FACTORIES``
(base tier) or ``DURABLE_BUS_FACTORIES`` (both tiers); nothing else in this
file changes.

Delivery timing differs between the two implementations and is the one thing
this contract does not constrain: ``InProcessEventBus.publish`` dispatches
synchronously, while ``JsonlEventBus.publish`` appends to the plan's log and
dispatches on ``drain(plan)``. ``_publish`` below is the single place that
knows the difference, so the assertions themselves stay bus-agnostic.
"""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.persistence
import pipeline.server
from pipeline.events import (
    EVENT_TYPES,
    EventBus,
    InProcessEventBus,
    JsonlEventBus,
    make_event,
)

PLAN = "contract-plan"

# Two distinct real event types, used wherever a test needs a type that has no
# subscriber to contrast against the type that does.
SUBSCRIBED_TYPE = "story_ready"
OTHER_TYPE = "parked"

# The keys ``make_event`` always stamps on an event, plus the four optional
# envelope fields it adds only when the caller supplies them (ADR 0010's
# event envelope, EV-01).
REQUIRED_KEYS = {"type", "plan", "story_key", "payload", "ts"}
OPTIONAL_KEYS = {"correlation_id", "event_id", "schema_version", "idempotency_key"}


# ---------------------------------------------------------------------------
# Bus construction
# ---------------------------------------------------------------------------
def _make_in_process(tmp_path):
    """Build the in-process bus (dispatches synchronously, keeps no log)."""
    return InProcessEventBus()


def _make_jsonl(tmp_path):
    """Build the JSONL bus rooted in a per-test directory.

    ``JsonlEventBus`` takes a *root directory*, not a file: it appends one
    ``<plan>.events.jsonl`` per plan inside that root, so a fresh empty
    directory per test keeps cases hermetic.
    """
    return JsonlEventBus(tmp_path / "events")


_NATS_SERVER = shutil.which("nats-server")
#: ``tests/conftest.py`` scrubs every ``PIPELINE_*`` variable before collection,
#: so the CI "a missing broker binary must fail, not skip" flag is also read
#: from the ambient marker GitHub Actions sets.
_REQUIRE_NATS = (
    os.environ.get("PIPELINE_REQUIRE_NATS_TESTS") == "1"
    or os.environ.get("GITHUB_ACTIONS") == "true"
)
#: A tree without a broker binary skips the NATS cases -- except where the
#: caller demanded them, so a CI run can never pass by skipping them.
_NATS_ABSENT = pytest.mark.skipif(
    _NATS_SERVER is None and not _REQUIRE_NATS,
    reason="nats-server not installed",
)


def _free_port():
    """A loopback port that was bound and released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start_nats_server():
    """Start a private ``nats-server -js``; return its ``(url, process)``."""
    if _NATS_SERVER is None:
        pytest.fail(
            "PIPELINE_REQUIRE_NATS_TESTS=1 but no nats-server binary is on PATH"
        )
    port = _free_port()
    store = tempfile.mkdtemp(prefix="pipeline-contract-nats-")
    server = subprocess.Popen(
        [_NATS_SERVER, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", store],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_until_listening(port, server)
    return f"nats://127.0.0.1:{port}", server


def _stop_nats_server(server):
    """Terminate a server started by :func:`_start_nats_server`."""
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
        server.kill()


def _new_nats_bus(url, *, publish_timeout_s=None):
    """Build a ``NatsEventBus`` aimed at *url*, owning a stream of its own.

    The stream name is unique per bus, so cases never read one another's
    stored messages.
    """
    os.environ["PIPELINE_NATS_URL"] = url
    os.environ["PIPELINE_NATS_STREAM"] = f"CONTRACT_{os.getpid()}_{_free_port()}"
    os.environ["PIPELINE_NATS_MANAGE_STREAM"] = "1"
    os.environ["PIPELINE_EVENT_PUBLISH_TIMEOUT_S"] = str(
        5 if publish_timeout_s is None else publish_timeout_s
    )
    for name in (
        "PIPELINE_NATS_CREDS_FILE",
        "PIPELINE_NATS_TLS_CA_FILE",
        "PIPELINE_NATS_DURABLE_TYPES",
    ):
        os.environ.pop(name, None)

    from pipeline.events_nats import NatsEventBus

    return NatsEventBus()


def _make_nats_durable(root, *, url=None, publish_timeout_s=None):
    """Build a NATS bus for the publish-side durable cases.

    *root* is unused: this bus writes no plan sidecar. A case that supplies
    *url* aims the bus at that address -- so a case naming an address with no
    broker behind it gets a bus whose ``publish`` must report the failure --
    and every other case gets a private server that stops with the bus.
    """
    if url is None:
        url, server = _start_nats_server()
    else:
        server = None
    bus = _new_nats_bus(url, publish_timeout_s=publish_timeout_s)
    close_bus = bus.close

    def close_bus_and_server():
        close_bus()
        if server is not None:
            _stop_nats_server(server)

    bus.close = close_bus_and_server
    return bus


def _make_nats(tmp_path):
    """Build a NATS JetStream bus against a private, disposable server.

    One ``nats-server -js`` per bus (random loopback port, private store
    directory) keeps every case hermetic and leaves no state behind: the
    returned bus's ``close`` also stops the server it started.
    """
    return _make_nats_durable(tmp_path)


def _wait_until_listening(port, server, timeout=20.0):
    """Block until the server accepts connections and answers JetStream."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            pytest.fail(
                f"nats-server exited with {server.returncode} before listening"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"nats-server did not listen on 127.0.0.1:{port}")


BUS_FACTORIES = [
    pytest.param(_make_in_process, id="in-process"),
    pytest.param(_make_jsonl, id="jsonl"),
    pytest.param(
        _make_nats,
        id="nats",
        marks=pytest.mark.skipif(
            _NATS_SERVER is None and not _REQUIRE_NATS,
            reason="nats-server not installed",
        ),
    ),
]


@pytest.fixture(params=BUS_FACTORIES)
def bus_factory(request, tmp_path):
    """A freshly built bus of each implementation in ``BUS_FACTORIES``.

    Function-scoped, so every parametrized case gets its own bus and its own
    ``tmp_path``: no handler, log file or counter is shared between cases.
    """
    bus = request.param(tmp_path)
    try:
        yield bus
    finally:
        close = getattr(bus, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Test doubles and helpers
# ---------------------------------------------------------------------------
def _recorder():
    """A subscriber that records the events it receives in ``.calls``.

    A plain function, not a callable object: the bus logs ``handler.__qualname__``
    on a handler error, so a handler without one would raise from the error
    path itself and mask what the test is checking.
    """
    calls = []

    def handler(event):
        calls.append(event)

    handler.calls = calls
    return handler


def _raiser(message="handler failed on purpose"):
    """A subscriber that always raises, for the handler-isolation case."""

    def handler(event):
        raise RuntimeError(message)

    return handler


def _publish(bus, event):
    """Deliver *event* through *bus* to its subscribers.

    The one place that knows the two delivery models: a bus that persists to a
    log (it exposes ``drain``) dispatches when the log is drained, a bus that
    dispatches synchronously (it does not) is done when ``publish`` returns.
    """
    bus.publish(event)
    drain = getattr(bus, "drain", None)
    if callable(drain):
        drain(event["plan"])


def _rebuilt_event(event):
    """Rebuild an event from its wire form via the documented inverse.

    An event is a plain dict, so the inverse of ``make_event`` is
    ``make_event(**fields)`` over the fields read back off the wire; ``ts`` is
    carried on the envelope rather than restamped, which is what makes this a
    round-trip check and not a re-creation.
    """
    wire = json.loads(json.dumps(event))
    rebuilt = make_event(
        wire["type"],
        wire["plan"],
        wire.get("story_key"),
        wire.get("payload"),
        wire.get("correlation_id"),
        event_id=wire.get("event_id"),
        schema_version=wire.get("schema_version"),
        idempotency_key=wire.get("idempotency_key"),
    )
    rebuilt["ts"] = wire["ts"]
    return rebuilt


def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll *predicate* until it is true, failing after *timeout* seconds.

    Durable delivery is asynchronous, so every durable assertion is made
    through this helper instead of a fixed ``sleep()``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), f"condition not met within {timeout}s"


# ---------------------------------------------------------------------------
# Base tier -- every bus implementation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("event_type", sorted(EVENT_TYPES))
def test_publish_reaches_subscribers_of_the_published_type_only(
    bus_factory, event_type
):
    """An event reaches subscribers of its own type, and no other subscriber."""
    assert isinstance(bus_factory, EventBus)
    other_type = OTHER_TYPE if event_type != OTHER_TYPE else SUBSCRIBED_TYPE
    assert other_type in EVENT_TYPES and other_type != event_type

    recorded = _recorder()
    also_recorded = _recorder()
    bus_factory.subscribe(event_type, recorded)
    bus_factory.subscribe(other_type, also_recorded)

    event = make_event(event_type, PLAN)
    _publish(bus_factory, event)

    assert recorded.calls == [event], (
        f"a subscriber of {event_type!r} must receive exactly the one event "
        f"published for it, got {recorded.calls!r}"
    )
    assert also_recorded.calls == [], (
        f"a subscriber of {other_type!r} must not receive an event of type "
        f"{event_type!r}, got {also_recorded.calls!r}"
    )


def test_make_event_rejects_a_type_outside_event_types():
    """An event type outside ``EVENT_TYPES`` is rejected where events are made."""
    bogus_type = "definitely-not-an-event-type"
    assert bogus_type not in EVENT_TYPES

    with pytest.raises(ValueError):
        make_event(bogus_type, PLAN)


def test_delivers_reflects_subscription_state_without_side_effects(bus_factory):
    """``delivers()`` answers "will anything receive this type?" and changes nothing."""
    assert SUBSCRIBED_TYPE in EVENT_TYPES and OTHER_TYPE in EVENT_TYPES

    assert not bus_factory.delivers(SUBSCRIBED_TYPE), (
        "a bus with no subscribers must not claim to deliver anything"
    )
    # Reading it twice must stay a read: an implementation (or a test) that
    # treats delivers() as "ensure a subscriber exists" registers something
    # here and then delivers the event twice, which the length check below
    # catches.
    assert not bus_factory.delivers(SUBSCRIBED_TYPE)

    recorded = _recorder()
    bus_factory.subscribe(SUBSCRIBED_TYPE, recorded)
    assert bus_factory.delivers(SUBSCRIBED_TYPE), (
        "a type with a live subscriber must report as delivered"
    )
    assert not bus_factory.delivers(OTHER_TYPE), (
        f"{OTHER_TYPE!r} has no subscriber and must not report as delivered"
    )

    event = make_event(SUBSCRIBED_TYPE, PLAN)
    _publish(bus_factory, event)

    assert len(recorded.calls) == 1, (
        "reading delivers() must not add a subscriber: the event was delivered "
        f"{len(recorded.calls)} times instead of once"
    )
    assert recorded.calls == [event]


def test_minimal_event_dict_round_trips_unchanged():
    """A minimal event keeps its exact key set and values across a round trip."""
    event = make_event(SUBSCRIBED_TYPE, PLAN)
    assert set(event) == REQUIRED_KEYS, (
        f"a minimal event carries exactly {sorted(REQUIRED_KEYS)}, got {sorted(event)}"
    )

    first = dict(event)
    assert json.loads(json.dumps(first)) == first, "the wire form must not drop keys"
    second = _rebuilt_event(event)
    assert second == first, "reconstruction must reproduce the original event"
    third = _rebuilt_event(second)
    assert third == first, "a second round trip must still equal the original event"


def test_event_with_optional_envelope_fields_round_trips_unchanged():
    """Every EV-01 optional envelope field survives a round trip with its value."""
    event = make_event(
        SUBSCRIBED_TYPE,
        PLAN,
        story_key="BR-02",
        payload={"nested": {"a": [1, 2, 3]}, "flag": False},
        correlation_id="corr-1",
        event_id="evt-1",
        schema_version=2,
        idempotency_key="idem-1",
    )
    expected_keys = REQUIRED_KEYS | OPTIONAL_KEYS
    assert set(event) == expected_keys, (
        f"an event built with every optional field carries {sorted(expected_keys)}, "
        f"got {sorted(event)}"
    )

    first = dict(event)
    second = _rebuilt_event(event)
    assert set(second) == expected_keys, (
        "reconstruction silently dropped optional fields: "
        f"expected {sorted(expected_keys)}, got {sorted(second)}"
    )
    assert second == first, "reconstruction must reproduce every key and value"
    third = _rebuilt_event(second)
    assert third == first, "a second round trip must still equal the original event"

    # A truncated timestamp is invisible to a "two round trips agree" check, so
    # compare the original's own encoding: microsecond precision must survive.
    assert datetime.fromisoformat(first["ts"]).isoformat() == first["ts"], (
        f"the timestamp lost precision across the round trip: {first['ts']!r}"
    )


def test_handler_exception_does_not_stop_later_handlers(bus_factory):
    """A raising handler neither silences later handlers nor breaks the bus."""
    bus_factory.subscribe(SUBSCRIBED_TYPE, _raiser())
    recorded = _recorder()
    bus_factory.subscribe(SUBSCRIBED_TYPE, recorded)

    first = make_event(SUBSCRIBED_TYPE, PLAN)
    second = make_event(SUBSCRIBED_TYPE, PLAN, story_key="BR-02")

    # Whether publish propagates a handler error is not part of the contract,
    # so swallow either outcome; what follows is the contract.
    with contextlib.suppress(Exception):
        _publish(bus_factory, first)

    assert recorded.calls == [first], (
        "a handler registered after a raising one must still receive the event, "
        f"got {recorded.calls!r}"
    )

    _publish(bus_factory, second)

    assert recorded.calls == [first, second], (
        "the bus must keep working after a handler raised; expected both events, "
        f"got {recorded.calls!r}"
    )


# ---------------------------------------------------------------------------
# Durable tier -- broker-backed implementations only
# ---------------------------------------------------------------------------
# Durable bus implementations that deliver into this process register here --
# the cases that need a consumer runner (redelivery, dead-lettering). Each
# entry is a factory ``factory(root, *, url=None, publish_timeout_s=None) ->
# EventBus`` whose keyword arguments override the adapter's broker address and
# publish timeout.
DURABLE_BUS_FACTORIES: list = []

# Durable bus implementations whose *publish* side alone is under test:
# deduplication of a republished event and the bounded publish timeout. These
# cases never wait for a delivery, so a bus that only publishes (and leaves
# consumption to its own consumer process) belongs here.
PUBLISH_ONLY_DURABLE_BUS_FACTORIES: list = [
    pytest.param(
        _make_nats_durable,
        id="nats",
        marks=_NATS_ABSENT,
    ),
]


@pytest.fixture
def durable_bus_factory(request, tmp_path, monkeypatch):
    """A freshly built durable bus, with plan sidecars pointed at *tmp_path*.

    The plan directory is redirected so a dead-letter record written by the
    bus lands in the test's temporary directory instead of the operator's
    plans directory.
    """
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    bus = request.param(tmp_path)
    try:
        yield bus
    finally:
        close = getattr(bus, "close", None)
        if callable(close):
            close()


def _dead_letter_records(plan, event_id):
    """The ``event_dead_lettered`` sidecar records written for *event_id*.

    ADR 0010 rule 1: a dead-lettered event produces an
    ``event_dead_lettered`` notification written directly to the plan's
    sidecar rather than through the bus that failed. The sidecar is the plan's
    notifications JSONL, whose record builder stamps the notification's own
    name in ``event``, the suppressed/deduplicated event id in ``dedup_key``
    and the delivery attempt count in ``attempt``.
    """
    path = pipeline.persistence._notifications_jsonl_path(plan)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") != "event_dead_lettered":
            continue
        if record.get("dedup_key") != event_id:
            continue
        records.append(record)
    return records


def _event_publish_error():
    """The publish-timeout error type raised by a durable bus adapter.

    Imported lazily: the durable tier must collect in a tree whose broker
    adapter is not importable, and only a case that has a durable
    implementation registered reaches this helper.
    """
    from pipeline.events_nats import EventPublishError

    return EventPublishError


@pytest.mark.durable_bus
@pytest.mark.parametrize("durable_bus_factory", DURABLE_BUS_FACTORIES, indirect=True)
def test_redelivery_after_a_raising_handler(durable_bus_factory):
    """A handler that raises is retried with the same event, up to the limit."""
    calls = []

    def handler(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails on purpose")

    durable_bus_factory.subscribe(SUBSCRIBED_TYPE, handler)
    _publish(durable_bus_factory, make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-1"))

    _wait_for(lambda: len(calls) >= 2, timeout=5.0)

    assert len(calls) == 2, (
        "one raising attempt must be redelivered exactly once, handler ran "
        f"{len(calls)} times"
    )
    assert {call["event_id"] for call in calls} == {"e-1"}, (
        "redelivery must carry the same event id, got "
        f"{[call.get('event_id') for call in calls]!r}"
    )


@pytest.mark.durable_bus
@pytest.mark.parametrize("durable_bus_factory", DURABLE_BUS_FACTORIES, indirect=True)
def test_dead_letter_after_max_deliver_attempts(durable_bus_factory, monkeypatch):
    """An always-raising handler is dead-lettered at the delivery limit, once."""
    # Read per delivery attempt by the adapter, so setting it here is effective.
    monkeypatch.setenv("PIPELINE_EVENT_MAX_DELIVER", "2")
    calls = []

    def handler(event):
        calls.append(event)
        raise RuntimeError("always fails on purpose")

    durable_bus_factory.subscribe(SUBSCRIBED_TYPE, handler)

    durable_bus_factory.publish(make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-1"))
    _wait_for(lambda: _dead_letter_records(PLAN, "e-1"), timeout=5.0)

    assert len(calls) == 2, (
        f"the delivery limit is 2 attempts, handler ran {len(calls)} times"
    )
    records = _dead_letter_records(PLAN, "e-1")
    assert len(records) == 1, (
        f"exactly one dead-letter record for e-1, got {len(records)}"
    )
    assert records[0]["attempt"] == 2, (
        f"the record must carry the attempt count 2, got {records[0]['attempt']!r}"
    )

    # Dead-lettering is terminal: no further retries for e-1.
    time.sleep(0.3)
    assert len(calls) == 2, (
        f"no retries after dead-lettering, handler ran {len(calls)} times"
    )

    # A second event through the same handler must get its own full budget.
    durable_bus_factory.publish(make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-2"))
    _wait_for(lambda: _dead_letter_records(PLAN, "e-2"), timeout=5.0)

    assert len(calls) == 4, (
        "each event gets its own attempt budget: after e-1 (2 attempts) and "
        f"e-2 (2 attempts) the handler must have run 4 times, ran {len(calls)} times"
    )
    assert len(_dead_letter_records(PLAN, "e-2")) == 1, (
        "exactly one dead-letter record for e-2, got "
        f"{len(_dead_letter_records(PLAN, 'e-2'))}"
    )
    assert len(_dead_letter_records(PLAN, "e-1")) == 1, (
        "e-1 must keep exactly one dead-letter record after e-2 is processed, got "
        f"{len(_dead_letter_records(PLAN, 'e-1'))}"
    )


@pytest.mark.durable_bus
@pytest.mark.parametrize(
    "durable_bus_factory", PUBLISH_ONLY_DURABLE_BUS_FACTORIES, indirect=True
)
def test_duplicate_publish_is_suppressed(durable_bus_factory):
    """A republished event id delivers once; a genuinely new event still delivers."""
    recorded = _recorder()
    durable_bus_factory.subscribe(SUBSCRIBED_TYPE, recorded)

    first = make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-1", idempotency_key="k-1")
    durable_bus_factory.publish(first)
    durable_bus_factory.publish(dict(first))
    _wait_for(lambda: recorded.calls, timeout=5.0)
    time.sleep(0.3)

    assert len(recorded.calls) == 1, (
        "a duplicate publish of the same event id must deliver once, delivered "
        f"{len(recorded.calls)} times"
    )

    fresh = make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-2", idempotency_key="k-2")
    durable_bus_factory.publish(fresh)
    _wait_for(lambda: len(recorded.calls) >= 2, timeout=5.0)

    assert len(recorded.calls) == 2, (
        "suppression must be keyed on the event id, not a single seen-any-event "
        f"latch: expected 2 deliveries, got {len(recorded.calls)}"
    )


# Parametrized directly, not through the fixture: this case needs the factory
# itself so it can build a bus aimed at a closed port instead of using the
# fixture's healthy bus.
@pytest.mark.durable_bus
@pytest.mark.parametrize("durable_factory", PUBLISH_ONLY_DURABLE_BUS_FACTORIES)
def test_publish_raises_after_timeout_when_broker_unreachable(
    durable_factory, tmp_path
):
    """``publish`` is bounded: an unreachable broker fails fast, it never hangs."""
    bus = durable_factory(
        tmp_path, url="nats://127.0.0.1:1", publish_timeout_s=0.2
    )
    started = time.monotonic()
    try:
        with pytest.raises(_event_publish_error()):
            bus.publish(make_event(SUBSCRIBED_TYPE, PLAN, event_id="e-1"))
    finally:
        close = getattr(bus, "close", None)
        if callable(close):
            close()

    elapsed = time.monotonic() - started
    assert elapsed < 5.0, (
        "publish must give up after its publish timeout instead of retrying "
        f"forever; it took {elapsed:.3f}s"
    )
