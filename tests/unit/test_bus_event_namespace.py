"""Bus events and their consumers act in the namespace of the plan they are about.

On a deployment with team namespaces a record written from a bus event must
land in the namespace bound when the event was made, not in the default. The
NATS consumer delivers on its own thread with nothing bound, so the sink and
the dead-letter writer must carry the namespace on the event itself.

Covered here:

* ``make_event`` stamps the bound namespace, and only a non-default one;
* a notification published in a team namespace and delivered from an unbound
  context (the consumer thread) lands in that namespace's history;
* a malformed namespace on a notification event is refused, never defaulted;
* the dead-letter record is written to and deduplicated in its namespace;
* plan completion reads, writes and marks the plan in the bound namespace, and
  two namespaces holding a plan of the same name complete independently.

The record store is the real in-memory store; only the bus is stubbed.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import (
    event_wiring,
    events,
    events_nats_consumer,
    notification_sinks,
    persistence,
    plan_completion,
    plan_guidance,
    server,
)
from pipeline.actor import bind_namespace
from pipeline.store_records import InvalidRecordKeyError

ACME_WEB = "acme/web"
ACME_API = "acme/api"
PLAN = "cap1"


class _Bus:
    """Records every publish; reports ``notification`` as delivered."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, event: dict) -> None:
        self.published.append(event)

    def delivers(self, event_type: str) -> bool:
        return event_type == "notification"


class _TeamRecords:
    """An in-memory record view that serves every namespace, as a team deployment does.

    The reference in-memory store refuses plan completion outside the default
    namespace, and the file and sqlite views refuse every other namespace, so
    none of them can show what a team namespace holds.
    """

    def __init__(self) -> None:
        self.history: dict[tuple[str, str], list[dict]] = {}
        self.completed: set[tuple[str, str]] = set()
        self.reports: dict[tuple[str, str], str] = {}

    def append_notification(self, namespace, plan_name, record):
        self.history.setdefault((namespace, plan_name), []).append(dict(record))

    def get_notification_history(self, namespace, plan_name):
        return list(self.history.get((namespace, plan_name), [])), 0

    def get_journal(self, namespace, plan_name, story_key):
        return []

    def is_plan_completed(self, namespace, plan_name):
        return (namespace, plan_name) in self.completed

    def mark_plan_completed(self, namespace, plan_name):
        if (namespace, plan_name) in self.completed:
            return False
        self.completed.add((namespace, plan_name))
        return True

    def put_plan_report(self, namespace, plan_name, text):
        self.reports[(namespace, plan_name)] = text

    def get_plan_report(self, namespace, plan_name):
        return self.reports.get((namespace, plan_name))


class _Store:
    """The shape ``server._store`` and ``build_store()`` return: ``records()``."""

    def __init__(self, records: _TeamRecords) -> None:
        self._records = records

    def records(self) -> _TeamRecords:
        return self._records


@pytest.fixture
def records(monkeypatch, tmp_path):
    view = _TeamRecords()
    monkeypatch.setattr(server, "_store", _Store(view))
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return view


@pytest.fixture
def bus(monkeypatch):
    recorder = _Bus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: recorder)
    return recorder


def _history(records: _TeamRecords, namespace: str, plan: str = PLAN):
    return records.get_notification_history(namespace, plan)[0]


# ---------------------------------------------------------------------------
# make_event stamps the bound namespace
# ---------------------------------------------------------------------------


def test_make_event_stamps_the_bound_namespace_when_none_is_passed():
    with bind_namespace(ACME_WEB):
        event = events.make_event("notification", PLAN)

    assert event["namespace"] == ACME_WEB


def test_make_event_without_a_bound_namespace_has_no_namespace_key():
    event = events.make_event("notification", PLAN)

    assert "namespace" not in event


def test_make_event_bound_to_the_default_namespace_has_no_namespace_key():
    with bind_namespace("default"):
        event = events.make_event("notification", PLAN)

    assert "namespace" not in event


def test_make_event_keeps_an_explicit_namespace_over_the_bound_one():
    with bind_namespace(ACME_WEB):
        event = events.make_event("notification", PLAN, namespace="a/b")

    assert event["namespace"] == "a/b"


def test_make_event_refuses_an_explicit_namespace_that_is_not_valid():
    with bind_namespace(ACME_WEB), pytest.raises(InvalidRecordKeyError):
        events.make_event("notification", PLAN, namespace="../escape")


# ---------------------------------------------------------------------------
# A notification published in a team namespace lands there, not in default
# ---------------------------------------------------------------------------


def test_notification_published_in_a_team_namespace_lands_there_from_an_unbound_sink(
    records, bus
):
    with bind_namespace(ACME_WEB):
        persistence._notify_user(PLAN, "gate failed", event="ci_pending_stalled")
    event = bus.published[0]

    # The consumer thread delivers with nothing bound.
    notification_sinks.file_log_sink(event)

    assert len(_history(records, ACME_WEB)) == 1
    assert _history(records, "default") == []


def test_notification_published_unbound_lands_in_the_default_namespace(records, bus):
    persistence._notify_user(PLAN, "gate failed", event="ci_pending_stalled")
    event = bus.published[0]

    notification_sinks.file_log_sink(event)

    assert len(_history(records, "default")) == 1
    assert _history(records, ACME_WEB) == []


@pytest.mark.parametrize("namespace", ["", 7, ["acme/web"]])
def test_sink_refuses_a_malformed_namespace_and_writes_nothing(
    records, caplog, namespace
):
    event = {
        "type": "notification",
        "plan": PLAN,
        "ts": "2026-01-02T03:04:05+00:00",
        "namespace": namespace,
        "payload": {"message": "gate failed", "event": "ci_pending_stalled"},
    }

    with caplog.at_level(logging.ERROR):
        notification_sinks.file_log_sink(event)

    assert _history(records, "default") == []
    assert _history(records, ACME_WEB) == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Dead-letter records follow the event's namespace
# ---------------------------------------------------------------------------


def test_dead_letter_record_is_written_and_deduplicated_in_the_event_namespace(records):
    event = {"story_key": "S1", "namespace": ACME_WEB}

    events_nats_consumer._write_dead_letter_record("agent_done", PLAN, event, "evt-1", 3)
    events_nats_consumer._write_dead_letter_record("agent_done", PLAN, event, "evt-1", 3)

    assert len(_history(records, ACME_WEB)) == 1
    assert _history(records, "default") == []


def test_dead_letter_record_for_a_malformed_namespace_falls_back_to_default(records):
    event = {"story_key": "S1", "namespace": 7}

    events_nats_consumer._write_dead_letter_record("agent_done", PLAN, event, "evt-2", 3)

    assert len(_history(records, "default")) == 1


def test_dead_letter_record_without_a_namespace_is_written_to_default(records):
    events_nats_consumer._write_dead_letter_record(
        "agent_done", PLAN, {"story_key": "S1"}, "evt-3", 3
    )

    assert len(_history(records, "default")) == 1
    assert _history(records, ACME_WEB) == []


# ---------------------------------------------------------------------------
# Other publishers carry the bound namespace
# ---------------------------------------------------------------------------


def test_plan_guidance_resolved_published_in_a_team_namespace_carries_it(bus):
    with bind_namespace(ACME_WEB):
        plan_guidance._publish_resolved(PLAN)

    assert bus.published[0]["namespace"] == ACME_WEB


def test_plan_guidance_resolved_published_unbound_has_no_namespace_key(bus):
    plan_guidance._publish_resolved(PLAN)

    assert "namespace" not in bus.published[0]


def test_ci_complete_reconcile_inside_a_team_namespace_carries_it():
    with bind_namespace(ACME_WEB):
        event = events.make_event("reconcile", PLAN, payload={"reason": "ci_complete"})

    assert event["namespace"] == ACME_WEB


def test_ci_complete_reconcile_inside_the_default_namespace_has_no_namespace_key():
    with bind_namespace("default"):
        event = events.make_event("reconcile", PLAN, payload={"reason": "ci_complete"})

    assert "namespace" not in event


def test_agent_dispatched_inside_a_team_namespace_carries_it():
    with bind_namespace(ACME_WEB):
        event = events.make_event("agent_dispatched", PLAN, story_key="S1")

    assert event["namespace"] == ACME_WEB


# ---------------------------------------------------------------------------
# Plan completion works in the plan's namespace
# ---------------------------------------------------------------------------


@pytest.fixture
def completion(records, bus, monkeypatch):
    monkeypatch.setattr(plan_completion, "build_store", lambda *a, **k: _Store(records))
    monkeypatch.setattr(
        plan_completion,
        "format_plan_summary",
        lambda plan_name, manifest, history: "SUMMARY",
    )
    return records


DONE_MANIFEST = {"stories": {"S1": {"status": "done", "title": "story S1"}}}


def _deliver(bus: _Bus) -> None:
    """Hand every published notification to the sink, as the consumer does."""
    for event in bus.published:
        if event["type"] == "notification":
            notification_sinks.file_log_sink(event)


def test_completion_in_a_team_namespace_marks_and_notifies_only_there(completion, bus):
    with bind_namespace(ACME_WEB):
        assert plan_completion.notify_if_plan_completed(PLAN, DONE_MANIFEST) is True
    _deliver(bus)

    assert completion.is_plan_completed(ACME_WEB, PLAN)
    assert completion.get_plan_report(ACME_WEB, PLAN) is not None
    assert len(_history(completion, ACME_WEB)) == 1
    assert not completion.is_plan_completed("default", PLAN)
    assert completion.get_plan_report("default", PLAN) is None
    assert _history(completion, "default") == []


def test_second_completion_call_in_the_same_namespace_returns_false(completion, bus):
    with bind_namespace(ACME_WEB):
        plan_completion.notify_if_plan_completed(PLAN, DONE_MANIFEST)
        assert plan_completion.notify_if_plan_completed(PLAN, DONE_MANIFEST) is False
    _deliver(bus)

    assert len(_history(completion, ACME_WEB)) == 1


def test_same_named_plan_in_another_namespace_completes_independently(completion, bus):
    with bind_namespace(ACME_WEB):
        plan_completion.notify_if_plan_completed(PLAN, DONE_MANIFEST)

    assert not completion.is_plan_completed(ACME_API, PLAN)
    with bind_namespace(ACME_API):
        assert plan_completion.notify_if_plan_completed(PLAN, DONE_MANIFEST) is True
