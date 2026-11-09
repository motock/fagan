"""The notifier enqueues the outbox entry itself, before it publishes it.

ADR 0012 decision 9: "Events are transport; the outbox is the durable record
... written in the same transaction as the state change that caused it."

The contract under test:

* ``pipeline.persistence._notify_user`` writes the outbox entry itself,
  through the process's shared record store
  (``_store.records().append_outbox(DEFAULT_NAMESPACE, plan, channel, record)``),
  on the CALLING thread, BEFORE it publishes the bus event.  The event on the
  bus is transport only.
* The record that is enqueued is exactly the published event: the line in
  ``<plan>.outbox.jsonl`` is ``json.dumps(event, default=str)``.
* ``pipeline.notification_outbox.outbox_sink`` stays the public enqueue entry
  point (same signature, never raises) and is now the writer that goes
  through ``append_outbox``.  It is no longer a bus subscriber, so a bus
  delivery cannot write the outbox a second time.
* The legacy gate (``PIPELINE_NOTIFY_OUTBOX_ENABLED`` exactly ``"1"`` plus the
  ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` allowlist) selects the ``_legacy``
  channel; the notification routing config selects the instance channels.
* A failure to enqueue is logged (the error TYPE only) and never stops the
  notification history write or the publish.
* ``_plan_owner`` reads the manifest through the shared store
  (``_store.records().get_manifest(DEFAULT_NAMESPACE, plan)``), never through
  a second store connection that would contend for the caller's write lock.

Every assertion below goes through the public API: ``_notify_user``,
``outbox_sink`` and the record store's public surface.  Nothing here reads
implementation source text.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import threading
import traceback
from pathlib import Path

import pytest

from pipeline import (
    event_wiring,
    notification_outbox,
    paths,
    persistence,
    server,
)
from pipeline import (
    notification_channels as nc,
)
from pipeline.events import EventBus, InProcessEventBus
from pipeline.notification_outbox import outbox_sink
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, LEGACY_OUTBOX_CHANNEL

PLAN = "plan-a"
ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"
CONFIG_ENV = "PIPELINE_NOTIFY_CONFIG_PATH"
BUS_ENV = "PIPELINE_EVENT_BUS"

ALLOWLISTED = "plan_completed"
NOT_ALLOWLISTED = "story_parked"
INSTANCE = "email-a"
OWNER = "alice"

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The production line ceiling enforced by ``scripts/check_line_limit.py``.
PRODUCTION_LINE_CEILING = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeChannel:
    """A notification channel type that only has to exist for the config."""

    type_name = "fake_notify"

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        return cls(instance_name)

    def send(self, record):
        return None


def _write_config(monkeypatch, plan_dir, owner=None):
    """Write a notification config with one instance and one route."""
    monkeypatch.setattr(nc, "_REGISTRY", {"fake_notify": _FakeChannel})
    match = {"plan": f"{PLAN}-*"}
    if owner is not None:
        match["owner"] = owner
    config = {
        "version": 1,
        "instances": {INSTANCE: {"type": "fake_notify"}},
        "routes": [
            {
                "name": "team",
                "match": match,
                "deliver": [{"events": ["*"], "to": [INSTANCE]}],
            }
        ],
    }
    path = plan_dir / "notifications.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV, str(path))
    return path


class _SpyRecords(InMemoryRecordStore):
    """An in-memory record view that records every outbox append."""

    def __init__(self, order=None):
        super().__init__()
        self.order = order if order is not None else []
        self.appends: list[dict] = []
        self.manifest: dict | None = None
        self.transaction_depth = 0

    def get_manifest(self, namespace, plan_name):
        manifest = super().get_manifest(namespace, plan_name)
        return manifest if manifest is not None else self.manifest

    def append_outbox(self, namespace, plan_name, channel, record):
        self.order.append("append")
        self.appends.append(
            {
                "namespace": namespace,
                "plan": plan_name,
                "channel": channel,
                "record": copy.deepcopy(record),
                "thread": threading.get_ident(),
                "in_transaction": self.transaction_depth > 0,
            }
        )
        super().append_outbox(namespace, plan_name, channel, record)

    def transaction(self, namespace, plan_name, wait_seconds=0.0):
        inner = super().transaction(namespace, plan_name, wait_seconds)

        @contextlib.contextmanager
        def _wrapper():
            self.transaction_depth += 1
            try:
                with inner as acquired:
                    yield acquired
            finally:
                self.transaction_depth -= 1

        return _wrapper()


class _SpyStore:
    """The process's shared store: a record view plus the transaction API."""

    def __init__(self, order=None):
        self._records = _SpyRecords(order)

    @property
    def records_view(self) -> _SpyRecords:
        return self._records

    def records(self) -> _SpyRecords:
        return self._records

    def transaction(self, namespace, plan_name, wait_seconds=0.0):
        return self._records.transaction(namespace, plan_name, wait_seconds)


class _FailingStore(_SpyStore):
    """A shared store whose outbox append always raises."""

    def __init__(self, error, order=None):
        super().__init__(order)
        self.error = error

    def records(self) -> _SpyRecords:
        failing = self

        class _Failing(_SpyRecords):
            def append_outbox(self, namespace, plan_name, channel, record):
                failing.order.append("append")
                raise failing.error

        if not isinstance(self._records, _Failing):
            self._records = _Failing(failing.order)
        return self._records


class _RecordingBus(EventBus):
    """A bus with no local handlers that records what it published."""

    def __init__(self, order=None):
        self.order = order if order is not None else []
        self.published: list[dict] = []

    def publish(self, event):
        self.order.append("publish")
        self.published.append(event)

    def subscribe(self, event_type, handler):
        return None


class _RaisingBus(_RecordingBus):
    """A bus whose publish fails (a broker outage)."""

    def publish(self, event):
        super().publish(event)
        raise RuntimeError("broker down")


class _RemoteBus(_RecordingBus):
    """A broker-backed bus: a durable remote consumer, no local handlers."""

    def delivers(self, event_type):
        return True


def _error_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]


def _with_traceback(record) -> str:
    text = record.getMessage()
    if record.exc_info:
        text += "\n" + "".join(traceback.format_exception(*record.exc_info))
    return text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An isolated plans directory and a clean notification environment."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(notification_outbox, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path, raising=False)
    monkeypatch.delenv(ENABLED_ENV, raising=False)
    monkeypatch.delenv(EVENTS_ENV, raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.setenv(BUS_ENV, "inprocess")
    return tmp_path


@pytest.fixture
def file_store(plan_dir, monkeypatch):
    """The process's shared store: a real file store over the temp directory."""
    store = FileStore()
    monkeypatch.setattr(server, "_store", store)
    return store


@pytest.fixture
def spy_store(plan_dir, monkeypatch):
    """The process's shared store: an in-memory one that records appends."""
    store = _SpyStore()
    monkeypatch.setattr(server, "_store", store)
    return store


def _legacy_outbox(plan_dir) -> Path:
    return plan_dir / f"{PLAN}.outbox.jsonl"


def _history_log(plan_dir) -> Path:
    return plan_dir / f"{PLAN}.notifications.log"


# ---------------------------------------------------------------------------
# The notifier writes the outbox entry itself
# ---------------------------------------------------------------------------

def test_the_notifier_enqueues_the_outbox_entry_itself(plan_dir, file_store, monkeypatch):
    """One ``_notify_user`` call writes exactly one outbox line: the event.

    The bus here is a plain in-process bus with no outbox subscriber, so the
    line can only come from the notifier itself.
    """
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    published: list[dict] = []
    bus = InProcessEventBus()
    bus.subscribe("notification", published.append)
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    assert len(published) == 1, "the notifier must publish exactly one event"
    path = _legacy_outbox(plan_dir)
    assert path.exists(), "the notifier must write the outbox entry itself"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [json.dumps(published[0], default=str)]


def test_the_wired_bus_does_not_write_the_outbox_entry_twice(plan_dir, file_store, monkeypatch):
    """The wired bus writes the outbox once, not once per subscriber."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    monkeypatch.setattr(event_wiring, "_BUS", None)
    bus = event_wiring.get_bus()
    published: list[dict] = []
    bus.subscribe("notification", published.append)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    assert len(published) == 1
    lines = _legacy_outbox(plan_dir).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"the outbox entry must be written exactly once, got {lines!r}"
    assert lines[0] == json.dumps(published[0], default=str)


def test_the_outbox_entry_is_written_even_when_the_publish_fails(
    plan_dir, file_store, monkeypatch
):
    """A bus that raises on publish cannot lose the outbox entry."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    bus = _RaisingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    lines = _legacy_outbox(plan_dir).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "the outbox entry must survive a failed publish"
    assert lines[0] == json.dumps(bus.published[0], default=str)
    # the publish fallback still writes the notification history
    assert _history_log(plan_dir).exists()


def test_a_remote_bus_that_delivers_gets_the_outbox_entry_written_once(
    plan_dir, file_store, monkeypatch
):
    """A broker-backed bus (delivers, no local handlers) still gets the entry."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    bus = _RemoteBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    lines = _legacy_outbox(plan_dir).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "the outbox entry must be written exactly once"
    assert lines[0] == json.dumps(bus.published[0], default=str)
    # the bus delivers the event, so the direct-write fallback is skipped
    assert not _history_log(plan_dir).exists()


def test_the_direct_write_fallback_covers_the_history_only(
    plan_dir, file_store, monkeypatch
):
    """Nothing delivers -> the fallback writes the history, never a 2nd line."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    bus = _RecordingBus()  # no local handlers: nothing delivers
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    assert _history_log(plan_dir).exists()
    lines = _legacy_outbox(plan_dir).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "the fallback must not write a second outbox line"


def test_the_outbox_entry_is_enqueued_before_the_event_is_published(
    plan_dir, monkeypatch
):
    """The enqueue happens first: the transport event is published after it."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    order: list[str] = []
    store = _SpyStore(order)
    monkeypatch.setattr(server, "_store", store)
    bus = _RecordingBus(order)
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    assert order == ["append", "publish"], (
        f"the outbox entry must be enqueued before the publish, got {order!r}"
    )


# ---------------------------------------------------------------------------
# The gate, the allowlist and the routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "0", "true"])
def test_the_outbox_is_not_written_when_the_gate_is_disabled(
    plan_dir, spy_store, monkeypatch, value
):
    """The legacy gate is off by default and only the exact ``"1"`` opens it."""
    if value is None:
        monkeypatch.delenv(ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(ENABLED_ENV, value)
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    assert spy_store.records_view.appends == [], "a disabled gate must enqueue nothing"
    assert not _legacy_outbox(plan_dir).exists()
    # the fallback still covers the notification history
    assert _history_log(plan_dir).exists()


def test_an_event_outside_the_allowlist_reaches_the_routed_instance_only(
    plan_dir, spy_store, monkeypatch
):
    """A non-allowlisted event skips ``_legacy`` but still reaches an instance."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    _write_config(monkeypatch, plan_dir)
    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "story parked", event=NOT_ALLOWLISTED)

    channels = [entry["channel"] for entry in spy_store.records_view.appends]
    assert INSTANCE in channels, "the routed instance must still receive the event"
    assert LEGACY_OUTBOX_CHANNEL not in channels, (
        "an event outside the allowlist must not be spooled to the legacy queue"
    )


def test_the_plan_owner_is_read_through_the_shared_store(plan_dir, monkeypatch):
    """An owner-scoped route only matches when the owner comes from the store."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    _write_config(monkeypatch, plan_dir, owner=OWNER)
    store = _SpyStore()
    store.records_view.manifest = {"owner": OWNER}
    monkeypatch.setattr(server, "_store", store)
    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    channels = [entry["channel"] for entry in store.records_view.appends]
    assert INSTANCE in channels, (
        "the plan owner must be read through the shared record store"
    )


# ---------------------------------------------------------------------------
# Failures never take the notification down
# ---------------------------------------------------------------------------

def test_a_failing_enqueue_is_logged_with_the_error_type_only(
    plan_dir, monkeypatch, caplog
):
    """A raising append is logged by type; the history write and publish live on."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    store = _FailingStore(RuntimeError("secret-detail"))
    monkeypatch.setattr(server, "_store", store)
    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    with caplog.at_level(logging.ERROR):
        persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors, "a failed enqueue must be logged"
    assert any("RuntimeError" in _with_traceback(record) for record in errors), (
        "the failure must be logged with the error type"
    )
    assert not any("secret-detail" in record.getMessage() for record in errors), (
        "the failure must be logged with the error type only"
    )
    assert len(bus.published) == 1, "a failed enqueue must not stop the publish"
    assert _history_log(plan_dir).exists(), "a failed enqueue must not stop the history"


# ---------------------------------------------------------------------------
# The enqueue happens on the caller's thread, in the caller's transaction
# ---------------------------------------------------------------------------

def test_the_enqueue_happens_on_the_calling_thread_in_the_callers_transaction(
    plan_dir, monkeypatch
):
    """The append runs on the caller's thread, inside the caller's transaction."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)
    store = _SpyStore()
    monkeypatch.setattr(server, "_store", store)
    bus = _RecordingBus()
    monkeypatch.setattr(event_wiring, "_BUS", bus)

    with store.transaction(DEFAULT_NAMESPACE, PLAN):
        persistence._notify_user(PLAN, "plan finished", event=ALLOWLISTED)

    appends = store.records_view.appends
    assert len(appends) == 1, "exactly one outbox entry must be enqueued"
    append = appends[0]
    assert append["thread"] == threading.get_ident(), (
        "the outbox entry must be enqueued on the calling thread"
    )
    assert append["in_transaction"] is True, (
        "the outbox entry must be enqueued inside the caller's transaction"
    )
    assert append["namespace"] == DEFAULT_NAMESPACE
    assert append["plan"] == PLAN
    assert append["channel"] == LEGACY_OUTBOX_CHANNEL


# ---------------------------------------------------------------------------
# Negative: a malformed event
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plan", ["", None])
def test_a_falsy_plan_is_reported_and_writes_nothing(
    plan_dir, spy_store, monkeypatch, caplog, plan
):
    """A falsy plan is an error; nothing is enqueued and nothing is written."""
    monkeypatch.setenv(ENABLED_ENV, "1")
    monkeypatch.setenv(EVENTS_ENV, ALLOWLISTED)

    with caplog.at_level(logging.ERROR):
        outbox_sink({"type": "notification", "plan": plan, "payload": {"event": ALLOWLISTED}})

    assert spy_store.records_view.appends == []
    assert not _legacy_outbox(plan_dir).exists()
    errors = _error_messages(caplog)
    assert errors, "a falsy plan must be reported as an error"
    assert any("plan" in message.lower() for message in errors)


# ---------------------------------------------------------------------------
# The documented contract
# ---------------------------------------------------------------------------

def test_reference_documents_that_the_notifier_enqueues_before_it_publishes():
    """REFERENCE.md must state that the notifier enqueues before it publishes."""
    text = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split()).lower()
    assert "the notifier enqueues the outbox entry before it publishes" in normalized


@pytest.mark.parametrize(
    "module", ["pipeline/notification_outbox.py", "pipeline/event_wiring.py"]
)
def test_the_touched_modules_stay_under_the_production_line_ceiling(module):
    """Both modules must stay under the 1000-line production ceiling."""
    path = REPO_ROOT / module
    count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    assert count <= PRODUCTION_LINE_CEILING, (
        f"{module} is {count} lines, over the {PRODUCTION_LINE_CEILING}-line ceiling"
    )
