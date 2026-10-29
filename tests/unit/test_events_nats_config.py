"""Configuration-refusal tests for :mod:`pipeline.events_nats`.

These tests must run without a ``nats-server`` binary: they inject a fake
``nats`` module into ``sys.modules`` and assert that every refused
configuration raises *before* any socket is opened. The broker-backed
behaviour lives in ``tests/integration/test_nats_bus.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline.events import JsonlEventBus
from pipeline.events_nats import (
    EventPublishError,
    NatsEventBus,
    nats_settings_from_env,
)

LOCAL_URL = "nats://127.0.0.1:4222"
REMOTE_URL = "nats://nats.example.com:4222"

_NATS_ENV_VARS = (
    "PIPELINE_NATS_URL",
    "PIPELINE_NATS_CREDS_FILE",
    "PIPELINE_NATS_TLS_CA_FILE",
    "PIPELINE_NATS_STREAM",
    "PIPELINE_EVENT_PUBLISH_TIMEOUT_S",
    "PIPELINE_NATS_MANAGE_STREAM",
    "PIPELINE_NATS_DURABLE_TYPES",
)


class _FakeNotFound(Exception):
    """Stand-in for ``nats.js.errors.NotFoundError``."""


class _FakeJetStream:
    """Records what the bus asks JetStream to do; answers like a real server."""

    def __init__(self) -> None:
        self.publish_calls: list[dict] = []
        self.stream_info_calls: list[str] = []
        self.add_stream_calls: list[dict] = []
        self.stream_missing = False
        self.publish_delay = 0.0
        self.publish_error: BaseException | None = None

    async def stream_info(self, name):
        self.stream_info_calls.append(name)
        if self.stream_missing:
            raise _FakeNotFound(f"stream {name} not found")
        return {"config": {"name": name}}

    async def add_stream(self, **kwargs):
        self.add_stream_calls.append(kwargs)
        return {"config": kwargs}

    async def publish(self, subject, payload=b"", headers=None, **kwargs):
        self.publish_calls.append(
            {"subject": subject, "payload": payload, "headers": headers}
        )
        if self.publish_delay:
            import asyncio

            await asyncio.sleep(self.publish_delay)
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
    """The fake ``nats`` module's ``connect``: records calls, never dials."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.connect_kwargs: dict = {}
        self.connect_error: Exception | None = None
        self.connect_delay = 0.0
        self.js = _FakeJetStream()

    async def connect(self, *args, **kwargs):
        self.connect_calls += 1
        self.connect_kwargs = kwargs
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error is not None:
            raise self.connect_error
        return _FakeNatsConnection(self.js)


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


# ---------------------------------------------------------------------------
# PIPELINE_NATS_URL
# ---------------------------------------------------------------------------
def test_missing_url_is_refused_before_connecting(clean_env, fake_nats):
    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "PIPELINE_NATS_URL" in str(excinfo.value)
    assert fake_nats.connect_calls == 0, (
        "a refused configuration must never open a socket"
    )


def test_plaintext_remote_url_without_a_ca_file_is_refused(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", REMOTE_URL)

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    message = str(excinfo.value)
    assert "PIPELINE_NATS_URL" in message
    assert "PIPELINE_NATS_TLS_CA_FILE" in message
    assert fake_nats.connect_calls == 0


def test_plaintext_remote_url_with_a_ca_file_is_accepted(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", REMOTE_URL)
    clean_env.setenv("PIPELINE_NATS_TLS_CA_FILE", "/etc/ca.pem")

    settings = nats_settings_from_env()

    assert settings.url == REMOTE_URL
    assert settings.tls_ca_file == "/etc/ca.pem"


def test_url_never_echoes_credentials(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", "nats://alice:hunter2@nats.example.com:4222")

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "hunter2" not in str(excinfo.value)
    assert "PIPELINE_NATS_URL" in str(excinfo.value)


# ---------------------------------------------------------------------------
# PIPELINE_NATS_CREDS_FILE
# ---------------------------------------------------------------------------
def test_world_readable_creds_file_is_refused(clean_env, fake_nats, tmp_path):
    creds = tmp_path / "nats.creds"
    creds.write_text("creds")
    creds.chmod(0o640)
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_NATS_CREDS_FILE", str(creds))

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "PIPELINE_NATS_CREDS_FILE" in str(excinfo.value)
    assert creds.name not in str(excinfo.value)
    assert fake_nats.connect_calls == 0


def test_missing_creds_file_is_refused(clean_env, fake_nats, tmp_path):
    creds = tmp_path / "absent.creds"
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_NATS_CREDS_FILE", str(creds))

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "PIPELINE_NATS_CREDS_FILE" in str(excinfo.value)
    assert creds.name not in str(excinfo.value)


def test_owner_only_creds_file_is_accepted(clean_env, fake_nats, tmp_path):
    creds = tmp_path / "nats.creds"
    creds.write_text("creds")
    creds.chmod(0o600)
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_NATS_CREDS_FILE", str(creds))

    bus = NatsEventBus()

    assert bus is not None
    assert fake_nats.connect_calls == 1


# ---------------------------------------------------------------------------
# PIPELINE_EVENT_PUBLISH_TIMEOUT_S
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["0", "-1", "abc", ""])
def test_invalid_publish_timeout_is_refused(clean_env, raw):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", raw)

    with pytest.raises(ValueError) as excinfo:
        nats_settings_from_env()

    assert "PIPELINE_EVENT_PUBLISH_TIMEOUT_S" in str(excinfo.value)


def test_publish_timeout_defaults_to_five_seconds(clean_env):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)

    settings = nats_settings_from_env()

    assert settings.publish_timeout_s == 5.0


def test_publish_timeout_is_read_from_the_environment(clean_env):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "2.5")

    settings = nats_settings_from_env()

    assert settings.publish_timeout_s == 2.5


# ---------------------------------------------------------------------------
# PIPELINE_NATS_STREAM / PIPELINE_NATS_MANAGE_STREAM
# ---------------------------------------------------------------------------
def test_missing_stream_without_the_manage_flag_is_refused(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    fake_nats.js.stream_missing = True

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "PIPELINE_NATS_STREAM" in str(excinfo.value)
    assert fake_nats.js.add_stream_calls == []


def test_missing_stream_is_created_when_management_is_enabled(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_NATS_MANAGE_STREAM", "1")
    fake_nats.js.stream_missing = True

    bus = NatsEventBus()

    assert bus is not None
    assert len(fake_nats.js.add_stream_calls) == 1
    created = fake_nats.js.add_stream_calls[0]
    assert created["subjects"] == ["pipeline.>"]
    assert created["max_age"] == 7 * 24 * 60 * 60
    assert created["storage"] == "file"


# ---------------------------------------------------------------------------
# Publish path (fake broker)
# ---------------------------------------------------------------------------
EVENT_TYPE = "agent_dispatched"
PLAN = "bus-plan"
PAYLOAD_MARKER = "payload-marker-do-not-log"


@pytest.fixture
def bus(clean_env, fake_nats):
    """A connected bus on a fake broker, torn down after the case."""
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    bus = NatsEventBus()
    try:
        yield bus
    finally:
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


def test_publish_uses_the_pipeline_subject(bus, fake_nats):
    bus.publish(_event())

    assert len(fake_nats.js.publish_calls) == 1
    assert fake_nats.js.publish_calls[0]["subject"] == f"pipeline.{EVENT_TYPE}.{PLAN}"


def test_publish_refuses_an_invalid_plan_before_sending(bus, fake_nats):
    # The JSONL bus owns the plan rules; the broker bus must not restate them.
    invalid_plan = "Bad/Plan"
    with pytest.raises(ValueError):
        JsonlEventBus._validate_plan(None, invalid_plan)

    with pytest.raises(ValueError):
        bus.publish(_event(plan=invalid_plan))

    assert fake_nats.js.publish_calls == [], (
        "an event whose plan is not a valid identifier must never reach the broker"
    )


def test_publish_sets_the_message_id_from_event_id(bus, fake_nats):
    bus.publish(_event(event_id="e-1"))

    headers = fake_nats.js.publish_calls[0]["headers"]
    assert headers["Nats-Msg-Id"] == "e-1"


def test_publish_falls_back_to_the_idempotency_key(bus, fake_nats):
    bus.publish(_event(idempotency_key="k-1"))

    headers = fake_nats.js.publish_calls[0]["headers"]
    assert headers["Nats-Msg-Id"] == "k-1"


def test_publish_prefers_the_event_id_over_the_idempotency_key(bus, fake_nats):
    bus.publish(_event(event_id="e-1", idempotency_key="k-1"))

    headers = fake_nats.js.publish_calls[0]["headers"]
    assert headers["Nats-Msg-Id"] == "e-1"


def test_publish_sends_no_message_id_without_an_event_identity(bus, fake_nats):
    bus.publish(_event())

    headers = fake_nats.js.publish_calls[0]["headers"] or {}
    assert "Nats-Msg-Id" not in headers, (
        "minting an id (or defaulting to an empty one) would defeat the stream's "
        "deduplication, which keys on the event's own identity"
    )


def test_publish_failure_is_reported_without_the_payload(bus, fake_nats):
    fake_nats.js.publish_error = TimeoutError("no ack")

    with pytest.raises(EventPublishError) as excinfo:
        bus.publish(_event())

    message = str(excinfo.value)
    assert EVENT_TYPE in message
    assert PAYLOAD_MARKER not in message


# ---------------------------------------------------------------------------
# Private loop lifecycle
# ---------------------------------------------------------------------------
def test_the_bus_owns_exactly_one_private_loop_thread(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    before = threading.active_count()

    bus = NatsEventBus()
    try:
        assert threading.active_count() == before + 1
        assert bus._thread.daemon is True
        assert bus._loop.is_running()

        first_loop = bus._loop
        bus.publish(_event(event_id="e-1"))
        bus.publish(_event(event_id="e-2"))

        assert bus._loop is first_loop, (
            "every publish must reuse the one loop: a loop per publish leaks a "
            "thread and a connection per event"
        )
        assert threading.active_count() == before + 1
    finally:
        bus.close()


def test_a_pid_change_rebuilds_the_loop_and_the_connection(
    clean_env, fake_nats, monkeypatch
):
    monkeypatch.setattr(os, "getpid", lambda: 1000)
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)

    bus = NatsEventBus()
    try:
        bus.publish(_event(event_id="e-1"))
        parent_loop, parent_thread = bus._loop, bus._thread
        assert bus._pid == 1000
        assert fake_nats.connect_calls == 1

        # The child of a fork() inherits the loop *object* but not the thread
        # that ran it, so the bus must treat the inherited pair as unusable.
        monkeypatch.setattr(os, "getpid", lambda: 1001)
        bus.publish(_event(event_id="e-2"))

        assert bus._loop is not parent_loop
        assert bus._thread is not parent_thread
        assert bus._pid == 1001
        assert fake_nats.connect_calls == 2
        assert len(fake_nats.js.publish_calls) == 2, (
            "the first publish after a pid change must actually send"
        )

        child_loop = bus._loop
        threads_after_child_build = threading.active_count()
        bus.publish(_event(event_id="e-3"))

        assert bus._loop is child_loop
        assert fake_nats.connect_calls == 2
        assert threading.active_count() == threads_after_child_build, (
            "the child must reuse the one loop it built, not build one per publish"
        )
    finally:
        bus.close()
        # The parent loop's thread is intentionally abandoned by the re-create
        # path (a real fork would not have it); stop it so the test process
        # does not carry a stray thread into the next case.
        parent_loop.call_soon_threadsafe(parent_loop.stop)
        parent_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Publish timeout
# ---------------------------------------------------------------------------
def test_publish_is_bounded_and_the_bus_stays_usable(clean_env, fake_nats):
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "0.2")

    bus = NatsEventBus()
    try:
        loop, thread, pid = bus._loop, bus._thread, bus._pid
        fake_nats.js.publish_delay = 10.0

        started = time.monotonic()
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event(event_id="e-1"))
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"publish waited {elapsed:.1f}s past its 0.2s budget"
        assert EVENT_TYPE in str(excinfo.value)
        assert PAYLOAD_MARKER not in str(excinfo.value)

        # A timed-out publish must leave the bus intact: stopping the loop,
        # dropping it, or latching a "broken" flag would make every later
        # publish fail too.
        fake_nats.js.publish_delay = 0.0
        bus.publish(_event(event_id="e-2"))

        assert bus._loop is loop
        assert bus._thread is thread
        assert bus._pid == pid
        assert fake_nats.connect_calls == 1
    finally:
        bus.close()


def test_a_broker_that_is_not_up_yet_defers_to_the_first_publish(
    clean_env, fake_nats
):
    """Constructing the bus validates settings, not the broker's availability.

    A broker that does not answer yet must leave the bus constructible: the
    failure is reported by ``publish`` as EventPublishError, the same typed
    error a publish timeout raises, so a call site that tolerates a failed
    publish tolerates an unreachable broker too instead of dying at
    construction.
    """
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    fake_nats.connect_error = OSError("connection refused")

    bus = NatsEventBus()
    try:
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event(event_id="e-1"))
    finally:
        bus.close()

    assert EVENT_TYPE in str(excinfo.value)
    assert PAYLOAD_MARKER not in str(excinfo.value)


def test_a_dialling_broker_is_bounded_by_the_publish_timeout(clean_env, fake_nats):
    """A broker that never answers must not outlast the publish timeout.

    The client's own reconnect loop is what has to be bounded here: waiting
    for it means one publish can hang for minutes, which no call site that
    tolerates a failed publish can absorb.
    """
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    clean_env.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "0.2")
    fake_nats.connect_delay = 30.0

    bus = NatsEventBus()
    try:
        started = time.monotonic()
        with pytest.raises(EventPublishError) as excinfo:
            bus.publish(_event(event_id="e-1"))
        elapsed = time.monotonic() - started
    finally:
        bus.close()

    assert elapsed < 10.0, f"publish waited {elapsed:.1f}s past its 0.2s budget"
    assert EVENT_TYPE in str(excinfo.value)
    assert PAYLOAD_MARKER not in str(excinfo.value)


