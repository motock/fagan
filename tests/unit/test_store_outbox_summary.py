"""The outbox summary every record store offers (ADR 0012).

A queue is one ``(namespace, plan, channel)`` outbox.  The summary a record
store returns describes the queues it holds: how many of them hold at least
one entry (``files``), how many entries they hold in total (``records``) and
how old the oldest entry with a usable timestamp is (``oldest_age_s``, or
``None`` when no entry had one).

The age rules are the ones :mod:`pipeline.outbox_health` uses, so a store and
the file view never disagree about how old a record is: an ISO-8601
timestamp, a naive one read as UTC, a missing, non-string or unparseable one
is an *unknown* age, and a timestamp ahead of the clock clamps to zero.

The behaviour mixin below is not named ``Test*``: pytest must never collect
it (it would call the unimplemented ``make_store``).  It is run against every
store, so a store that passes alone proves nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pipeline as pipeline_package
import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import outbox_health, paths, persistence
from pipeline.store import FileRecords, FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError
from pipeline.store_sqlite import SqliteRecords, SqliteStore

#: The plan the per-plan view asks for, and the plan it must not see.
PLAN = 'a'
OTHER_PLAN = 'ab'

#: Two channels of one plan: two queues.
CHANNEL = 'email-a'
SECOND_CHANNEL = 'email-b'

#: A distinctive plan and channel, used to prove the summary names neither.
NAMED_PLAN = 'tenant-plan-x'
NAMED_CHANNEL = 'chan-secret-y'

#: The payload text that must never reach a summary.
PAYLOAD = 'SECRET-PAYLOAD-XYZ'

#: The summary of a store that holds nothing.
EMPTY_SUMMARY = {"files": 0, "records": 0, "oldest_age_s": None}

#: A namespace no store can serve: it is not a valid namespace key.
INVALID_NAMESPACE = 'bad namespace!'

#: A plan name no store can serve: it is not a valid plan key.
INVALID_PLAN_NAME = 'bad/plan'


@pytest.fixture()
def plans_dir(tmp_path, monkeypatch):
    """Point every plans-directory binding at one fresh directory.

    The file-backed store writes its outbox spools under the instance
    channel's directory and reads the summary back from there, so every
    binding that names a plans directory has to point at the same place.
    """
    directory = tmp_path / 'plans'
    directory.mkdir()
    monkeypatch.setattr(paths, 'PLAN_DIR', directory)
    # ``FileRecords.outbox_summary`` resolves ``paths`` as a package
    # attribute (``from . import paths``); bind that attribute to the
    # module this fixture patches so a stray re-import elsewhere in the
    # worker cannot leave the store reading another module's PLAN_DIR.
    monkeypatch.setattr(pipeline_package, 'paths', paths)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', directory)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', directory)
    return directory


def _ts(age_s: float) -> str:
    """An ISO-8601 UTC timestamp that is *age_s* seconds old."""
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


def _naive_ts(age_s: float) -> str:
    """A naive ISO-8601 timestamp that is *age_s* seconds old."""
    return (
        (datetime.now(UTC) - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()
    )


class OutboxSummaryBehaviour:
    """The outbox-summary behaviour every record store must show."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    def _summary(self, store, plan_name=None):
        """The store's summary, for the whole namespace or for one plan."""
        if plan_name is None:
            return store.outbox_summary(DEFAULT_NAMESPACE)
        return store.outbox_summary(DEFAULT_NAMESPACE, plan_name=plan_name)

    def _assert_empty(self, summary) -> None:
        assert set(summary) == {"files", "records", "oldest_age_s"}
        assert summary["files"] == 0
        assert summary["records"] == 0
        assert summary["oldest_age_s"] is None

    # -- the empty store -----------------------------------------------
    def test_an_empty_store_is_the_empty_summary(self):
        store = self.make_store()

        self._assert_empty(self._summary(store))

    def test_the_summary_has_exactly_the_three_aggregate_keys(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(5)})

        summary = self._summary(store)

        assert set(summary) == {"files", "records", "oldest_age_s"}
        assert isinstance(summary["files"], int)
        assert isinstance(summary["records"], int)
        assert summary["oldest_age_s"] is None or isinstance(
            summary["oldest_age_s"], int
        )

    # -- depth ---------------------------------------------------------
    def test_two_queues_of_one_plan_count_two_files_and_three_records(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(30)})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(40)})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, SECOND_CHANNEL, {'ts': _ts(50)})

        summary = self._summary(store)

        assert summary["files"] == 2
        assert summary["records"] == 3

    def test_a_queue_of_another_plan_counts_separately(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(30)})
        store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, CHANNEL, {'ts': _ts(30)})

        summary = self._summary(store)

        assert summary["files"] == 2
        assert summary["records"] == 2

    # -- age -----------------------------------------------------------
    def test_the_oldest_age_is_the_largest_age(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(1200)})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, SECOND_CHANNEL, {'ts': _ts(5)})

        summary = self._summary(store)

        assert summary["oldest_age_s"] is not None
        assert 1200 <= summary["oldest_age_s"] <= 1260

    def test_a_fresh_record_alone_is_not_stale(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(5)})

        summary = self._summary(store)

        assert summary["records"] == 1
        assert summary["oldest_age_s"] is not None
        assert 0 <= summary["oldest_age_s"] < 120

    def test_a_record_without_a_timestamp_counts_but_has_no_age(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'event': 'x'})

        summary = self._summary(store)

        assert summary["records"] == 1
        assert summary["oldest_age_s"] is None

    def test_a_non_string_timestamp_is_an_unknown_age(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': 12345})

        summary = self._summary(store)

        assert summary["records"] == 1
        assert summary["oldest_age_s"] is None

    def test_an_unparseable_timestamp_is_an_unknown_age(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': 'garbage'})

        summary = self._summary(store)

        assert summary["records"] == 1
        assert summary["oldest_age_s"] is None

    def test_a_naive_timestamp_is_read_as_utc(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _naive_ts(1200)})

        summary = self._summary(store)

        assert summary["oldest_age_s"] is not None
        assert 1200 <= summary["oldest_age_s"] <= 1260

    def test_a_future_timestamp_clamps_to_zero(self):
        store = self.make_store()
        future = datetime.now(UTC) + timedelta(seconds=600)
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': future.isoformat()})

        summary = self._summary(store)

        assert summary["records"] == 1
        assert summary["oldest_age_s"] == 0

    # -- the per-plan view ---------------------------------------------
    def test_the_per_plan_view_excludes_another_plan(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(30)})
        store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, CHANNEL, {'ts': _ts(30)})

        summary = self._summary(store, plan_name=PLAN)

        assert summary["files"] == 1
        assert summary["records"] == 1

    def test_the_per_plan_view_of_an_unknown_plan_is_the_empty_summary(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(30)})

        self._assert_empty(self._summary(store, plan_name='no-such-plan'))

    # -- acking ---------------------------------------------------------
    def test_acking_everything_leaves_no_records_and_no_age(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(1200)})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(30)})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL)
        assert len(entries) == 2

        removed = store.ack_outbox(
            DEFAULT_NAMESPACE, PLAN, CHANNEL, [entry['id'] for entry in entries]
        )

        assert removed == 2
        summary = self._summary(store)
        assert summary["records"] == 0
        assert summary["oldest_age_s"] is None

    # -- the summary is aggregate only ----------------------------------
    def test_the_summary_carries_no_plan_channel_or_payload(self):
        store = self.make_store()
        store.append_outbox(
            DEFAULT_NAMESPACE,
            NAMED_PLAN,
            NAMED_CHANNEL,
            {'ts': _ts(1200), 'message': PAYLOAD},
        )

        dumped = json.dumps(self._summary(store))

        assert PAYLOAD not in dumped
        assert NAMED_PLAN not in dumped
        assert NAMED_CHANNEL not in dumped

    # -- rejected input --------------------------------------------------
    def test_an_invalid_plan_name_returns_the_empty_summary(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, CHANNEL, {'ts': _ts(1200)})

        summary = self._summary(store, plan_name=INVALID_PLAN_NAME)

        assert summary == EMPTY_SUMMARY

    def test_an_invalid_namespace_is_refused(self):
        store = self.make_store()

        with pytest.raises(InvalidRecordKeyError):
            store.outbox_summary(INVALID_NAMESPACE)


class TestInMemoryRecordStoreOutboxSummary(OutboxSummaryBehaviour):
    """The outbox-summary behaviour, run against the reference store."""

    def make_store(self):
        return InMemoryRecordStore()


class TestFileRecordsOutboxSummary(OutboxSummaryBehaviour):
    """The outbox-summary behaviour, run against the file-backed store."""

    @pytest.fixture(autouse=True)
    def _plans_dir(self, plans_dir):
        return plans_dir

    def make_store(self):
        return FileStore().records()


class TestSqliteRecordsOutboxSummary(OutboxSummaryBehaviour):
    """The outbox-summary behaviour, run against the sqlite-backed store."""

    @pytest.fixture(autouse=True)
    def _plans_dir(self, plans_dir, tmp_path):
        store = SqliteStore(str(tmp_path / 's.db'))
        self._store = store.records()
        try:
            yield self._store
        finally:
            store.close()

    def make_store(self):
        return self._store


class TestOutboxSummaryContract:
    """The contract the stores implement, and the file view's delegation."""

    def test_the_contract_module_declares_the_outbox_summary_mixin(self):
        from pipeline import store_contract_outbox

        assert isinstance(store_contract_outbox.OutboxSummaryContract, type)

    def test_every_store_offers_the_outbox_summary(self):
        for store_class in (InMemoryRecordStore, FileRecords, SqliteRecords):
            assert callable(getattr(store_class, 'outbox_summary', None))

    def test_the_file_store_reads_the_summary_through_the_outbox_health_module(
        self, plans_dir, monkeypatch
    ):
        calls = []

        def fake_summary(plan_dir):
            calls.append(Path(plan_dir))
            return {"files": 3, "records": 7, "oldest_age_s": 42}

        monkeypatch.setattr(outbox_health, "outbox_summary", fake_summary)
        store = FileStore().records()

        summary = store.outbox_summary(DEFAULT_NAMESPACE)

        assert summary == {"files": 3, "records": 7, "oldest_age_s": 42}
        assert len(calls) == 1
        assert Path(calls[0]) == plans_dir

    def test_the_file_store_reads_the_per_plan_view_through_the_outbox_health_module(
        self, plans_dir, monkeypatch
    ):
        calls = []

        def fake_summary_for_plan(plan_dir, plan_name):
            calls.append((Path(plan_dir), plan_name))
            return {"files": 1, "records": 1, "oldest_age_s": 5}

        monkeypatch.setattr(outbox_health, "outbox_summary_for_plan", fake_summary_for_plan)
        store = FileStore().records()

        summary = store.outbox_summary(DEFAULT_NAMESPACE, plan_name=PLAN)

        assert summary == {"files": 1, "records": 1, "oldest_age_s": 5}
        assert len(calls) == 1
        assert Path(calls[0][0]) == plans_dir
        assert calls[0][1] == PLAN
