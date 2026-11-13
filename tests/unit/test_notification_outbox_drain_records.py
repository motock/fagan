"""The notification outbox drain reads and acks queue entries through the store.

The drain used to read ``<plan>.outbox.jsonl`` off the disk and rewrite it in
place.  It now goes through the record store's per-channel outbox queues: the
legacy e-mail spool is the queue keyed by
:data:`pipeline.store_records.LEGACY_OUTBOX_CHANNEL`, and every configured
channel instance owns the queue keyed by its instance name.  Entries are read
with ``read_outbox`` and acknowledged with ``ack_outbox`` — never through a
second store connection, so a drain can never see a different copy of the
queue than the one the sink wrote.

Contract under test:

* ``drain_outbox(plan_name, sender) -> int`` drains the plan's legacy queue
  and returns the number of records the sender accepted.
* ``drain_channel_outboxes(plan_name=ALL_PLANS) -> dict[str, int]`` drains
  every configured instance's queue and returns the delivered counts keyed by
  instance name.
* A record the sender accepted is removed; a record it rejected, or one whose
  sender raised, is RETAINED for the next drain.
* A record older than :data:`notification_outbox.OUTBOX_MAX_AGE_S` is dropped
  UNSENT and reported with a single WARNING that names the plan and the
  channel only — never the record's content.
* A record whose timestamp is missing, unparseable or naive is KEPT (an odd
  timestamp must fail towards delivery, never towards silent loss).
* ``ALL_PLANS`` drains every plan's queue in one call.
* An instance that is no longer in the config is left untouched: its queue is
  neither drained nor acknowledged.
* The drain never raises: a store that blows up yields 0 / {} and an ERROR
  log carrying the error TYPE only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, LEGACY_OUTBOX_CHANNEL

LEGACY = LEGACY_OUTBOX_CHANNEL
REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "PAYLOAD-SECRET"


class _Store(InMemoryRecordStore):
    """An in-memory record store that also answers ``records()``.

    The live server store is reached as ``_store.records()``; the in-memory
    store implements the queue API directly, so this stand-in returns itself.
    """

    def records(self):
        return self


class _Sender:
    """Sender double: records what it received and returns a fixed verdict."""

    def __init__(self, verdict: bool = True) -> None:
        self.verdict = verdict
        self.received: list[dict] = []

    def __call__(self, record: dict) -> bool:
        self.received.append(record)
        return self.verdict


class _RaisingSender:
    """Sender double that always raises."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc or RuntimeError("sender exploded")
        self.received: list[dict] = []

    def __call__(self, record: dict) -> bool:
        self.received.append(record)
        raise self.exc


def _frozen_clock(moment: datetime) -> type[datetime]:
    """A ``datetime`` whose ``now()`` is exactly ``moment``."""
    moment = moment.astimezone(UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment

    return _Clock


@pytest.fixture(autouse=True)
def _isolate_breakers():
    """The per-channel circuit breaker is module state: reset it per test."""
    nc.reset_breakers()


@pytest.fixture
def store(monkeypatch):
    """Patch the live server store with an in-memory record store."""
    import pipeline.server as srv

    memory = _Store()
    monkeypatch.setattr(srv, "_store", memory)
    return memory


def _append(store, plan: str, channel: str, record: dict, namespace=DEFAULT_NAMESPACE):
    store.append_outbox(namespace, plan, channel, record)


def _queued(store, plan: str, channel: str, namespace=DEFAULT_NAMESPACE) -> list[dict]:
    """Return the records still pending in ``(plan, channel)``."""
    return [
        entry["record"]
        for entry in store.read_outbox(namespace, plan, channel)
    ]


def _ids(store, plan: str, channel: str, namespace=DEFAULT_NAMESPACE) -> list[str]:
    return [
        entry["id"] for entry in store.read_outbox(namespace, plan, channel)
    ]


def _record(message: str = "hello", ts: str | None = None) -> dict:
    record = {"event": "plan_completed", "plan": "demo", "message": message}
    if ts is not None:
        record["ts"] = ts
    return record


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


# --------------------------------------------------------------------------
# drain_outbox -- the legacy queue
# --------------------------------------------------------------------------


def test_should_remove_accepted_records_from_the_queue(store):
    _append(store, "demo", LEGACY, _record("one"))
    _append(store, "demo", LEGACY, _record("two"))
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 2
    assert len(sender.received) == 2
    assert _queued(store, "demo", LEGACY) == []


def test_should_retain_rejected_records_in_the_queue(store):
    _append(store, "demo", LEGACY, _record("nope"))
    sender = _Sender(False)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 0
    assert len(sender.received) == 1
    assert _queued(store, "demo", LEGACY) == [_record("nope")]


def test_should_retain_and_log_a_record_whose_sender_raises(store, caplog):
    _append(store, "demo", LEGACY, _record("boom"))
    sender = _RaisingSender(RuntimeError("sender exploded"))

    with caplog.at_level(logging.DEBUG):
        delivered = ob.drain_outbox("demo", sender)

    assert delivered == 0
    assert len(sender.received) == 1
    assert _queued(store, "demo", LEGACY) == [_record("boom")]
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_should_not_drain_another_plans_queue(store):
    _append(store, "demo", LEGACY, _record("mine"))
    _append(store, "other", LEGACY, _record("theirs"))
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert _queued(store, "demo", LEGACY) == []
    assert _queued(store, "other", LEGACY) == [_record("theirs")]


def test_should_return_zero_when_the_plan_has_no_queue(store):
    sender = _Sender(True)

    assert ob.drain_outbox("demo", sender) == 0
    assert sender.received == []


def test_should_drain_every_plan_for_all_plans(store):
    _append(store, "alpha", LEGACY, _record("a"))
    _append(store, "beta", LEGACY, _record("b"))
    _append(store, "gamma", LEGACY, _record("c"))
    sender = _Sender(True)

    delivered = ob.drain_outbox(ob.ALL_PLANS, sender)

    assert delivered == 3
    assert [rec["message"] for rec in sender.received] == ["a", "b", "c"]
    assert _queued(store, "alpha", LEGACY) == []
    assert _queued(store, "beta", LEGACY) == []
    assert _queued(store, "gamma", LEGACY) == []


def test_should_drain_only_the_legacy_queue_for_all_plans(store):
    _append(store, "demo", LEGACY, _record("legacy"))
    _append(store, "demo", "email-a", _record("instance"))
    sender = _Sender(True)

    delivered = ob.drain_outbox(ob.ALL_PLANS, sender)

    assert delivered == 1
    assert sender.received == [_record("legacy")]
    assert _queued(store, "demo", "email-a") == [_record("instance")]


def test_should_leave_another_namespace_untouched(store):
    _append(store, "demo", LEGACY, _record("mine"))
    _append(store, "demo", LEGACY, _record("theirs"), namespace="other")
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert _queued(store, "demo", LEGACY, namespace="other") == [_record("theirs")]


# --------------------------------------------------------------------------
# expiry
# --------------------------------------------------------------------------


def test_should_drop_an_expired_record_without_sending_it(store, caplog):
    stale = _record(SECRET, _iso(timedelta(seconds=ob.OUTBOX_MAX_AGE_S + 1)))
    _append(store, "demo", LEGACY, stale)
    sender = _Sender(True)

    with caplog.at_level(logging.WARNING):
        delivered = ob.drain_outbox("demo", sender)

    assert delivered == 0
    assert sender.received == []
    assert _queued(store, "demo", LEGACY) == []
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "demo" in message
    assert "legacy" in message.lower()
    assert SECRET not in caplog.text


def test_should_keep_a_record_at_the_max_age_boundary(store, monkeypatch):
    """A record exactly ``OUTBOX_MAX_AGE_S`` old is still delivered."""
    moment = datetime.now(UTC)
    monkeypatch.setattr(ob, "datetime", _frozen_clock(moment))
    at_limit = _record(
        "at-limit", (moment - timedelta(seconds=ob.OUTBOX_MAX_AGE_S)).isoformat()
    )
    _append(store, "demo", LEGACY, at_limit)
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert sender.received == [at_limit]
    assert _queued(store, "demo", LEGACY) == []


def test_should_keep_a_record_just_inside_the_max_age(store):
    inside = _record("inside", _iso(timedelta(seconds=ob.OUTBOX_MAX_AGE_S - 60)))
    _append(store, "demo", LEGACY, inside)
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert sender.received == [inside]


def test_should_drop_only_the_expired_record(store):
    stale = _record("stale", _iso(timedelta(days=8)))
    fresh = _record("fresh", _iso(timedelta(minutes=1)))
    _append(store, "demo", LEGACY, stale)
    _append(store, "demo", LEGACY, fresh)
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert sender.received == [fresh]
    assert _queued(store, "demo", LEGACY) == []


def test_should_treat_a_naive_timestamp_as_utc(store):
    naive = datetime.now(UTC).replace(tzinfo=None)
    expired = (naive - timedelta(seconds=ob.OUTBOX_MAX_AGE_S + 60)).isoformat()
    kept = (naive - timedelta(minutes=1)).isoformat()
    _append(store, "demo", LEGACY, _record("old", expired))
    _append(store, "demo", LEGACY, _record("new", kept))
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert [rec["message"] for rec in sender.received] == ["new"]


@pytest.mark.parametrize("ts", [None, "not-a-timestamp", "", 12345])
def test_should_keep_a_record_with_a_missing_or_unparseable_timestamp(store, ts):
    record = _record("odd")
    if ts is not None:
        record["ts"] = ts
    _append(store, "demo", LEGACY, record)
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert sender.received == [record]
    assert _queued(store, "demo", LEGACY) == []


# --------------------------------------------------------------------------
# drain_channel_outboxes -- the per-instance queues
# --------------------------------------------------------------------------


class _FakeChannel:
    """A channel instance double: DELIVERED unless it is told to raise."""

    type_name = "fake_drain_records"
    raising: ClassVar[set[str]] = set()

    def __init__(self, name: str) -> None:
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self.received: list[dict] = []

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        return cls(instance_name)

    def send(self, record):
        if self.name in type(self).raising:
            raise RuntimeError("channel exploded")
        self.received.append(record)
        return nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Install a two-instance notification config; return the config path."""
    monkeypatch.setattr(nc, "_REGISTRY", {"fake_drain_records": _FakeChannel})
    monkeypatch.setattr(_FakeChannel, "raising", set())
    cfg = tmp_path / "cfg" / "notifications.json"
    cfg.parent.mkdir()
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": {
                    "email-a": {"type": "fake_drain_records"},
                    "slack-a": {"type": "fake_drain_records"},
                },
                "routes": [
                    {
                        "name": "team",
                        "match": {"plan": "*"},
                        "deliver": [{"events": ["*"], "to": ["email-a", "slack-a"]}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(cfg))
    return cfg


def test_should_drain_every_configured_instance_queue(store, config):
    _append(store, "demo", "email-a", _record("mail"))
    _append(store, "demo", "slack-a", _record("chat"))

    delivered = ob.drain_channel_outboxes()

    assert delivered == {"email-a": 1, "slack-a": 1}
    assert _queued(store, "demo", "email-a") == []
    assert _queued(store, "demo", "slack-a") == []


def test_should_drain_the_instance_queue_of_every_plan_for_all_plans(store, config):
    _append(store, "alpha", "email-a", _record("a"))
    _append(store, "beta", "email-a", _record("b"))

    delivered = ob.drain_channel_outboxes()

    assert delivered["email-a"] == 2
    assert not delivered.get("slack-a")
    assert _queued(store, "alpha", "email-a") == []
    assert _queued(store, "beta", "email-a") == []


def test_should_drain_only_the_named_plan_queue(store, config):
    _append(store, "alpha", "email-a", _record("a"))
    _append(store, "beta", "email-a", _record("b"))

    delivered = ob.drain_channel_outboxes("alpha")

    assert delivered["email-a"] == 1
    assert not delivered.get("slack-a")
    assert _queued(store, "alpha", "email-a") == []
    assert _queued(store, "beta", "email-a") == [_record("b")]


def test_should_leave_an_instance_removed_from_the_config_untouched(store, config):
    _append(store, "demo", "email-a", _record("mail"))
    _append(store, "demo", "gone-a", _record("orphan"))
    before = _ids(store, "demo", "gone-a")

    ob.drain_channel_outboxes()

    assert _queued(store, "demo", "gone-a") == [_record("orphan")]
    assert _ids(store, "demo", "gone-a") == before


def test_should_retain_an_instance_record_whose_channel_raises(store, config, monkeypatch):
    monkeypatch.setattr(_FakeChannel, "raising", {"slack-a"})
    _append(store, "demo", "slack-a", _record("chat"))

    delivered = ob.drain_channel_outboxes()

    assert not delivered.get("slack-a")
    assert _queued(store, "demo", "slack-a") == [_record("chat")]


def test_should_not_let_one_instance_block_another(store, config, monkeypatch):
    monkeypatch.setattr(_FakeChannel, "raising", {"slack-a"})
    _append(store, "demo", "slack-a", _record("chat"))
    _append(store, "demo", "email-a", _record("mail"))

    delivered = ob.drain_channel_outboxes()

    assert delivered["email-a"] == 1
    assert _queued(store, "demo", "email-a") == []
    assert _queued(store, "demo", "slack-a") == [_record("chat")]


def test_should_return_empty_counts_without_a_config_file(store, monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(tmp_path / "absent.json"))
    _append(store, "demo", "email-a", _record("mail"))

    assert ob.drain_channel_outboxes() == {}
    assert _queued(store, "demo", "email-a") == [_record("mail")]


# --------------------------------------------------------------------------
# the drain never raises
# --------------------------------------------------------------------------


def test_should_not_raise_and_log_the_error_type_when_the_store_fails(
    store, caplog, monkeypatch
):
    def _boom(*args, **kwargs):
        raise RuntimeError("store secret detail")

    monkeypatch.setattr(store, "read_outbox", _boom)
    monkeypatch.setattr(store, "list_outboxes", _boom)
    monkeypatch.setattr(store, "ack_outbox", _boom)
    sender = _Sender(True)

    with caplog.at_level(logging.ERROR):
        delivered = ob.drain_outbox("demo", sender)

    assert delivered == 0
    assert sender.received == []
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in messages
    assert "store secret detail" not in messages


def test_should_not_raise_and_log_the_error_type_when_the_store_fails_for_channels(
    store, config, caplog, monkeypatch
):
    def _boom(*args, **kwargs):
        raise RuntimeError("store secret detail")

    monkeypatch.setattr(store, "read_outbox", _boom)
    monkeypatch.setattr(store, "list_outboxes", _boom)
    monkeypatch.setattr(store, "ack_outbox", _boom)

    with caplog.at_level(logging.ERROR):
        delivered = ob.drain_channel_outboxes()

    assert delivered == {}
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in messages
    assert "store secret detail" not in messages


# --------------------------------------------------------------------------
# structural requirements
# --------------------------------------------------------------------------


def test_should_have_removed_the_channel_outbox_path_helper():
    """``notification_channels.channel_outbox_path`` is gone for good."""
    assert not hasattr(nc, "channel_outbox_path")


def test_should_keep_the_outbox_suffix_on_the_channel_protocol():
    """The plugin-facing ``outbox_suffix`` attribute stays."""
    assert "outbox_suffix" in nc.Channel.__annotations__
    assert hasattr(nc, "as_sender")


def test_should_not_keep_a_plan_dir_binding_in_the_outbox_module():
    """The legacy spool resolves through ``persistence``, so ``notification_outbox`` holds no binding."""
    assert not hasattr(ob, "PLAN_DIR")


def test_should_read_and_ack_only_through_the_shared_store(store, monkeypatch):
    """The drain must not build a second store connection."""
    import pipeline.store as store_module

    def _no_second_store(*args, **kwargs):
        raise AssertionError("drain must not call build_store()")

    monkeypatch.setattr(store_module, "build_store", _no_second_store)
    monkeypatch.setattr(ob, "build_store", _no_second_store, raising=False)
    _append(store, "demo", LEGACY, _record("one"))
    sender = _Sender(True)

    assert ob.drain_outbox("demo", sender) == 1
    assert _queued(store, "demo", LEGACY) == []


def test_should_keep_a_record_appended_between_read_and_ack(store, monkeypatch):
    """A record spooled while the drain was sending survives the ack."""
    _append(store, "demo", LEGACY, _record("first"))
    original_read = store.read_outbox

    def _read_then_append(namespace, plan_name, channel, limit=None):
        entries = original_read(namespace, plan_name, channel, limit)
        _append(store, plan_name, channel, _record("late"))
        return entries

    monkeypatch.setattr(store, "read_outbox", _read_then_append)
    sender = _Sender(True)

    delivered = ob.drain_outbox("demo", sender)

    assert delivered == 1
    assert _queued(store, "demo", LEGACY) == [_record("late")]


def test_reference_documents_the_store_backed_drain():
    """REFERENCE.md pins the store-backed drain wording."""
    text = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    assert "reads every plan's outbox queue from the store" in text
