"""Tests that a usage record reaches the record store, not the file sidecar.

``pipeline.usage_record.record_usage`` used to append the per-plan JSONL sidecar
itself (``pipeline.persistence._write_usage_record``).  Under ADR 0012 the usage
record is a *store record*: it is appended through the store's namespaced record
view, exactly like the outbox.  So a record the store refuses leaves no trace
anywhere -- no row, no file, no telemetry export.

The seams these tests use:

* ``pipeline.server._store`` -- the live store binding, read at call time, so
  patching that attribute is what makes a store visible to ``record_usage``.
* ``pipeline.telemetry_usage.record`` -- the telemetry export, patched to a
  recorder so "was this record exported?" is observable.
* ``pipeline.persistence.PLAN_DIR`` -- the file store's plans directory, kept on
  ``tmp_path`` so a file written by any path lands in the test's own directory.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path

import pytest

# pipeline.server is imported explicitly (the idiom in
# tests/unit/test_usage_record.py): a new test file that imports a pipeline.*
# module settles the pre-existing import cycle by importing it first.
import pipeline.persistence
import pipeline.server
import pipeline.telemetry_usage
import pipeline.usage_record
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, RecordStoreError

#: The attributes every ``logging.LogRecord`` carries; anything else on a
#: captured record is a structured field the record chose to publish.
_STANDARD_RECORD_FIELDS = set(
    logging.LogRecord("x", logging.WARNING, "p", 1, "m", None, None).__dict__
) | {"message", "asctime"}


class _RecordingStore:
    """A store that records every usage append, and the thread it happened on."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.threads: list[threading.Thread] = []

    def records(self) -> _RecordingStore:
        return self

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        self.events.append("append")
        self.threads.append(threading.current_thread())

    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        return []

    @contextlib.contextmanager
    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        yield True


class _RaisingStore:
    """A store whose usage append always fails."""

    def records(self) -> _RaisingStore:
        return self

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        raise RuntimeError("store is down")

    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        return []

    @contextlib.contextmanager
    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        yield True


@pytest.fixture(autouse=True)
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every file written by any code path inside the test's directory."""
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryRecordStore:
    """Install an in-memory record store as the live server store."""
    memory = InMemoryRecordStore()
    monkeypatch.setattr(pipeline.server, "_store", memory)
    return memory


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every usage record handed to the telemetry exporter."""
    exported: list[dict] = []
    monkeypatch.setattr(pipeline.telemetry_usage, "record", exported.append)
    return exported


def _stored(store: InMemoryRecordStore, plan: str) -> list[dict]:
    """Return the usage records the store holds for *plan*."""
    return store.get_usage(DEFAULT_NAMESPACE, plan)


def test_one_call_stores_one_record_and_exports_it_once(
    store: InMemoryRecordStore, telemetry: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One model call -> one stored record -> one telemetry export."""
    sidecar_writes: list[tuple] = []
    monkeypatch.setattr(
        pipeline.persistence,
        "_write_usage_record",
        lambda plan, record: sidecar_writes.append((plan, record)),
    )

    with pipeline.usage_record.usage_scope("plan-a", "story-1", attempt=3):
        pipeline.usage_record.record_usage(
            role="dispatch",
            backend="claude",
            model="claude-sonnet-4-5",
            input_tokens=1200,
            output_tokens=340,
            cache_creation_input_tokens=11,
            cache_read_input_tokens=22,
            cost_usd=0.0421,
            duration_ms=9876,
        )

    records = _stored(store, "plan-a")
    assert len(records) == 1
    record = records[0]
    assert set(record) == set(pipeline.usage_record.USAGE_FIELDS)
    assert record["plan"] == "plan-a"
    assert record["story_key"] == "story-1"
    assert record["attempt"] == 3
    assert record["role"] == "dispatch"
    assert record["backend"] == "claude"
    assert record["model"] == "claude-sonnet-4-5"
    assert record["input_tokens"] == 1200
    assert record["output_tokens"] == 340
    assert record["cache_creation_input_tokens"] == 11
    assert record["cache_read_input_tokens"] == 22
    assert record["cost_usd"] == pytest.approx(0.0421)
    assert record["duration_ms"] == 9876
    assert isinstance(record["ts"], str) and record["ts"]

    # Exactly one export, of exactly the record that was stored.
    assert len(telemetry) == 1
    assert telemetry[0] == record

    # The record went through the store, not straight to the file sidecar.
    assert sidecar_writes == []


def test_two_calls_store_two_records_in_order(store: InMemoryRecordStore) -> None:
    """Exactly one record per model call, appended in call order."""
    with pipeline.usage_record.usage_scope("plan-b", "story-2", attempt=1):
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=0, cost_usd=0
        )
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=7
        )

    records = _stored(store, "plan-b")
    assert len(records) == 2
    assert [r["input_tokens"] for r in records] == [0, 7]
    assert records[0]["cost_usd"] == 0
    assert records[0]["duration_ms"] is None


def test_no_scope_stores_nothing_and_exports_nothing(
    store: InMemoryRecordStore, telemetry: list[dict], plan_dir: Path
) -> None:
    """An unattributable call is not a record: nothing stored, nothing exported."""
    assert pipeline.usage_record.current_scope() is None

    pipeline.usage_record.record_usage(
        role="dispatch", backend="claude", model="m", input_tokens=5
    )

    assert store.list_plans(DEFAULT_NAMESPACE) == []
    assert telemetry == []
    assert list(plan_dir.iterdir()) == []


def test_explicit_scope_wins_over_the_bound_scope(
    store: InMemoryRecordStore, telemetry: list[dict]
) -> None:
    """An explicit ``scope=`` is the attribution, even inside a bound scope."""
    explicit = {"plan": "drained", "story_key": "s-drained", "attempt": 9}

    with pipeline.usage_record.usage_scope("bound", "s-bound", attempt=1):
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=4, scope=explicit
        )

    assert _stored(store, "bound") == []
    drained = _stored(store, "drained")
    assert len(drained) == 1
    assert drained[0]["story_key"] == "s-drained"
    assert drained[0]["attempt"] == 9
    assert len(telemetry) == 1


@pytest.mark.parametrize("plan", [None, "../x"])
def test_a_rejected_plan_is_dropped_with_one_warning_and_no_export(
    store: InMemoryRecordStore,
    telemetry: list[dict],
    caplog: pytest.LogCaptureFixture,
    plan_dir: Path,
    plan: str | None,
) -> None:
    """A record the store refuses is dropped: no row, no file, no export.

    The warning carries the error *type* only -- never the plan value, which
    may be a path.
    """
    scope = {"plan": plan, "story_key": "s-1", "attempt": 1}

    with (
        caplog.at_level(logging.WARNING, logger="pipeline.usage_record"),
        pipeline.usage_record.usage_scope("plan-ok", "s-ok", attempt=1),
    ):
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=3, scope=scope
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    warning = warnings[0]
    fields = {k: v for k, v in vars(warning).items() if k not in _STANDARD_RECORD_FIELDS}
    # Exactly one structured field: the error type.  No plan, no record.
    assert set(fields) == {"error_type"}
    assert isinstance(fields["error_type"], str) and fields["error_type"]
    # The offending value is never echoed into the log.
    assert str(plan) not in warning.getMessage()

    # Nothing was stored under the rejected plan, and nothing was exported.
    assert _stored(store, "plan-ok") == []
    assert telemetry == []
    assert list(plan_dir.iterdir()) == []


def test_a_rejected_plan_names_the_record_store_error(
    store: InMemoryRecordStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning's ``error_type`` names the store's own rejection error."""
    scope = {"plan": "../x", "story_key": "s-1", "attempt": 1}

    with caplog.at_level(logging.WARNING, logger="pipeline.usage_record"):
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=3, scope=scope
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    error_type = getattr(warnings[0], "error_type", None)
    assert isinstance(error_type, str)
    # The store refused the key; the warning names that failure, not a
    # synthetic one of the caller's own.
    assert error_type in {
        exc.__name__ for exc in RecordStoreError.__subclasses__()
    } | {"InvalidRecordKeyError", "InvalidUsageRecordError"}


def test_a_raising_store_never_raises_out_of_record_usage(
    monkeypatch: pytest.MonkeyPatch, telemetry: list[dict]
) -> None:
    """A store failure is dropped, not propagated: accounting never breaks work."""
    monkeypatch.setattr(pipeline.server, "_store", _RaisingStore())

    with pipeline.usage_record.usage_scope("plan-x", "story-x", attempt=1):
        assert (
            pipeline.usage_record.record_usage(
                role="dispatch", backend="claude", model="m", input_tokens=1
            )
            is None
        )

    # The record was never stored, so it must not be exported either.
    assert telemetry == []


def test_the_append_joins_a_held_transaction_on_the_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The append happens on the calling thread, inside the held transaction.

    It is stored before it is exported, so telemetry never reports a record the
    store did not take.
    """
    recorder = _RecordingStore()
    monkeypatch.setattr(pipeline.server, "_store", recorder)
    monkeypatch.setattr(
        pipeline.telemetry_usage,
        "record",
        lambda record: recorder.events.append("telemetry"),
    )

    with recorder.transaction(DEFAULT_NAMESPACE, "plan-t"):
        with pipeline.usage_record.usage_scope("plan-t", "story-t", attempt=1):
            pipeline.usage_record.record_usage(
                role="dispatch", backend="claude", model="m", input_tokens=1
            )
        # The append joined the transaction that was already held, and it came
        # first.  The export is not deferred to commit -- the store contract
        # has no commit hook -- so it has already run by the time the append
        # returns; what is guaranteed is the order, so no record the store did
        # not take is ever exported.
        assert recorder.events[:1] == ["append"]

    assert recorder.events == ["append", "telemetry"]
    assert recorder.threads == [threading.current_thread()]


def test_the_served_model_fallback_is_stored_verbatim(
    store: InMemoryRecordStore, telemetry: list[dict]
) -> None:
    """The TELEMETRY_CONTRACT fallback example is stored exactly as reported.

    ``model`` is what was requested; ``served_model`` is what actually served
    the call.  Dropping or rewriting the served model destroys attribution
    exactly when a fallback happened.
    """
    with pipeline.usage_record.usage_scope("plan-fb", "story-fb", attempt=3):
        pipeline.usage_record.record_usage(
            role="dispatch",
            backend="claude",
            model="claude-sonnet-4-5",
            served_model="gpt-5.1-2026-09-01",
            input_tokens=1200,
            output_tokens=350,
            cost_usd=0.0031,
            duration_ms=2340,
        )

    records = _stored(store, "plan-fb")
    assert len(records) == 1
    record = records[0]
    assert record["model"] == "claude-sonnet-4-5"
    assert record["served_model"] == "gpt-5.1-2026-09-01"
    assert record["cost_usd"] == pytest.approx(0.0031)
    assert len(telemetry) == 1
    assert telemetry[0]["served_model"] == "gpt-5.1-2026-09-01"


def test_the_file_store_still_writes_the_sidecar_under_the_persistence_plan_dir(
    plan_dir: Path,
) -> None:
    """The file store keeps writing ``<plan>.usage.jsonl`` where it always did."""
    with pipeline.usage_record.usage_scope("plan-file", "story-file", attempt=1):
        pipeline.usage_record.record_usage(
            role="dispatch", backend="claude", model="m", input_tokens=3
        )

    sidecar = plan_dir / "plan-file.usage.jsonl"
    assert sidecar.exists()
    lines = [line for line in sidecar.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert '"plan": "plan-file"' in lines[0] or '"plan":"plan-file"' in lines[0]
