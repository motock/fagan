"""The outbox summary is read from the record store, not from a directory.

``/api/health``, the scheduler tick and the metrics scrape all read the
notification outbox through the record store now: the store owns the queues,
so the readers ask the store for the aggregate instead of walking a
directory that may not even be the store's.

The readers resolve the store lazily (``pipeline.server._store``), so a test
that patches that binding is honoured -- and a store that is patched in is the
store the readers see, whatever the directories on disk hold.
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
from pipeline import (
    health_freshness,
    metrics_exposition,
    outbox_health,
    paths,
    persistence,
    tick_checks,
)
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.unit._injected_state import host_at
from tests.unit._plan_dir_references import count_references
from tests.unit.test_plan_dir_allowlist import ceiling

#: The staleness threshold the readers apply.
THRESHOLD_S = 900

#: The plan the outbox entries are queued under.
PLAN = 'a'


class _MemoryStore(InMemoryRecordStore):
    """An in-memory record store that also answers the store seam.

    ``pipeline.server._store`` is a *store*: the readers ask it for its
    record view and read the summary from there.  This stand-in is the
    in-memory record store itself, so a reader that asks the store for its
    record view and a reader that asks the store directly see the same
    queues.
    """

    def records(self):
        return self

    def outbox_summary(self, namespace, plan_name=None):
        return super().outbox_summary(namespace, plan_name)


def _ts(age_s: float) -> str:
    """An ISO-8601 UTC timestamp that is *age_s* seconds old."""
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


@pytest.fixture()
def outbox_store(monkeypatch):
    """Patch ``pipeline.server._store`` with an in-memory record store."""
    store = _MemoryStore()
    monkeypatch.setattr(pipeline.server, '_store', store)
    return store


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory, so no reader falls back to a real one."""
    directory = tmp_path / 'plans'
    directory.mkdir()
    monkeypatch.setattr(paths, 'PLAN_DIR', directory)
    # The readers resolve ``paths`` as a package attribute; bind it to the
    # module patched above (see tests/unit/test_store_outbox_summary.py).
    monkeypatch.setattr(pipeline_package, 'paths', paths)
    return directory


class TestHealthExtrasReadsTheStore:
    """``health_extras`` grades the outbox the store reports."""

    def test_a_stale_outbox_in_the_store_is_unhealthy(self, outbox_store, plan_dir):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(1200)}
        )

        extras = health_freshness.health_extras(host_at(plan_dir), outbox_store)

        assert extras['healthy'] is False
        assert 'outbox_stale' in extras['degraded']
        assert extras['outbox']['records'] == 1
        assert extras['outbox']['oldest_age_s'] >= 1200

    def test_the_outbox_block_is_the_store_summary(self, outbox_store, plan_dir):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(30)}
        )

        extras = health_freshness.health_extras(host_at(plan_dir), outbox_store)

        assert extras['outbox'] == outbox_store.outbox_summary(DEFAULT_NAMESPACE)
        assert extras['healthy'] is True
        assert extras['degraded'] == []

    def test_an_outbox_exactly_at_the_threshold_stays_healthy(
        self, outbox_store, plan_dir
    ):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(THRESHOLD_S)}
        )

        extras = health_freshness.health_extras(host_at(plan_dir), outbox_store)

        assert extras['outbox']['oldest_age_s'] >= THRESHOLD_S
        assert extras['healthy'] is True
        assert 'outbox_stale' not in extras['degraded']

    def test_the_store_wins_over_a_spool_file_in_the_plan_dir(
        self, outbox_store, plan_dir
    ):
        # A spool file on disk that the store knows nothing about must not
        # reach the verdict: the store owns the queues.
        (plan_dir / 'ghost.outbox.jsonl').write_text(
            json.dumps({'ts': _ts(1200)}) + '\n', encoding='utf-8'
        )
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(30)}
        )

        extras = health_freshness.health_extras(host_at(plan_dir), outbox_store)

        assert extras['outbox']['records'] == 1
        assert extras['outbox']['oldest_age_s'] < THRESHOLD_S
        assert extras['healthy'] is True
        assert 'outbox_stale' not in extras['degraded']

    def test_the_scheduler_snapshot_is_still_read_from_the_plan_dir(
        self, outbox_store, plan_dir
    ):
        (plan_dir / health_freshness.HEALTH_FILENAME).write_text(
            json.dumps({'heartbeat_ts': datetime.now(UTC).isoformat()}),
            encoding='utf-8',
        )

        extras = health_freshness.health_extras(host_at(plan_dir), outbox_store)

        assert extras['scheduler_age_s'] is not None
        assert extras['scheduler_age_s'] < 60


class _FakeDaemon:
    def __init__(self) -> None:
        self.stale_calls = 0

    def _notify_if_checkout_stale(self, config) -> None:
        self.stale_calls += 1


class TestTickChecksReadsTheStore:
    """The scheduler tick grades the outbox the store reports."""

    @pytest.fixture(autouse=True)
    def _tick_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, 'PLAN_DIR', tmp_path)
        monkeypatch.setattr(pipeline_package, 'paths', paths)
        monkeypatch.delenv('PIPELINE_EVENT_BUS', raising=False)
        monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, str(THRESHOLD_S))
        monkeypatch.setattr(tick_checks, '_OUTBOX_STALE', False)
        (tmp_path / 'alpha.manifest.json').write_text('{}', encoding='utf-8')
        self.events = []

        def fake_notify(plan, message, **kwargs):
            self.events.append((plan, kwargs.get('event')))

        monkeypatch.setattr(persistence, '_notify_user', fake_notify)
        return tmp_path

    def test_one_stale_outbox_notifies_once_across_three_ticks(self, outbox_store):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(1200)}
        )
        daemon = _FakeDaemon()

        for _ in range(3):
            tick_checks.run_tick_checks(daemon, {})

        assert [event for _, event in self.events] == ['outbox_stale']
        assert daemon.stale_calls == 3

    def test_a_fresh_outbox_never_notifies(self, outbox_store):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(30)}
        )
        daemon = _FakeDaemon()

        for _ in range(3):
            tick_checks.run_tick_checks(daemon, {})

        assert self.events == []


class TestMetricsExpositionReadsTheStore:
    """The metrics scrape samples the outbox the store reports."""

    def test_the_outbox_samples_come_from_the_store(self, outbox_store, plan_dir):
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(1200)}
        )

        snapshot = metrics_exposition.collect(host_at(plan_dir), outbox_store)

        assert snapshot['fagan_outbox_records'] == [({}, 1)]
        assert snapshot['fagan_outbox_oldest_age_seconds'][0][1] >= 1200

    def test_the_store_wins_over_a_spool_file_in_the_plan_dir(
        self, outbox_store, plan_dir
    ):
        (plan_dir / 'ghost.outbox.jsonl').write_text(
            json.dumps({'ts': _ts(1200)}) + '\n', encoding='utf-8'
        )
        outbox_store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, 'email-a', {'ts': _ts(30)}
        )

        snapshot = metrics_exposition.collect(host_at(plan_dir), outbox_store)

        assert snapshot['fagan_outbox_records'] == [({}, 1)]
        assert snapshot['fagan_outbox_oldest_age_seconds'][0][1] < THRESHOLD_S


class TestApplyOutboxHealthAcceptsASummary:
    """``apply_outbox_health`` takes the summary it is handed."""

    def test_the_given_summary_is_the_outbox_block(self):
        summary = {'files': 2, 'records': 3, 'oldest_age_s': 1200}
        result = {'healthy': True, 'degraded': []}

        out = outbox_health.apply_outbox_health(
            result, '/nonexistent-plan-dir', threshold=THRESHOLD_S, summary=summary
        )

        assert out is result
        assert out['outbox'] == summary
        assert out['healthy'] is False
        assert out['degraded'] == ['outbox_stale']

    def test_a_fresh_summary_leaves_the_payload_healthy(self):
        summary = {'files': 1, 'records': 1, 'oldest_age_s': 30}
        result = {'healthy': True, 'degraded': []}

        out = outbox_health.apply_outbox_health(
            result, '/nonexistent-plan-dir', threshold=THRESHOLD_S, summary=summary
        )

        assert out['outbox'] == summary
        assert out['healthy'] is True
        assert out['degraded'] == []


def _plan_dir_references(module: str) -> int:
    """How many direct PLAN_DIR references *module* makes, counted as the guard counts them."""
    path = Path(__file__).resolve().parents[2] / module
    return count_references(path.read_text(encoding='utf-8'), module)


class TestPlanDirAllowlistCeilings:
    """The readers stop naming the plans directory themselves."""

    def test_health_freshness_no_longer_names_the_plans_directory(self):
        assert (
            _plan_dir_references('pipeline/health_freshness.py')
            <= ceiling('pipeline/health_freshness.py')
        )

    def test_tick_checks_no_longer_names_the_plans_directory(self):
        assert (
            _plan_dir_references('pipeline/tick_checks.py')
            <= ceiling('pipeline/tick_checks.py')
        )

    def test_metrics_exposition_no_longer_names_the_plans_directory(self):
        assert (
            _plan_dir_references('pipeline/metrics_exposition.py')
            <= ceiling('pipeline/metrics_exposition.py')
        )

    def test_outbox_health_stays_on_itsceiling(self):
        assert (
            _plan_dir_references('pipeline/outbox_health.py')
            <= ceiling('pipeline/outbox_health.py')
        )
