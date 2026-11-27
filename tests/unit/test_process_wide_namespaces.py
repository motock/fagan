"""The process-wide metrics scrape and health probe count every namespace.

``fagan_plans`` and the outbox gauges (and the health probe's outbox block)
used to read only the default namespace. They now cover the default namespace
plus every namespace the store reports as holding a plan. The labels stay
closed: a namespace or plan name never appears in a sample.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import health_freshness, metrics_exposition
from pipeline.audit_memory import InMemoryAuditLog
from pipeline.host_state import FileHostState
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

TEAM = "acme/web"
TEAM_PLAN = "team-plan"
DEFAULT_PLAN = "default-plan"


class _Store:
    """An injected store: its record view and an in-memory audit log."""

    def __init__(self, records) -> None:
        self._records = records

    def records(self):
        return self._records

    def audit_log(self) -> InMemoryAuditLog:
        return InMemoryAuditLog()


class _HiddenListing:
    """A record view that offers no ``list_namespaces`` capability."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def list_plans(self, namespace: str) -> list[str]:
        return self._records.list_plans(namespace)

    def get_manifest(self, namespace: str, plan_name: str):
        return self._records.get_manifest(namespace, plan_name)

    def outbox_summary(self, namespace: str, plan_name=None) -> dict:
        return self._records.outbox_summary(namespace, plan_name)


class _OutboxOnly:
    """A stand-in that answers nothing but ``outbox_summary(namespace)``."""

    def __init__(self, summary: dict) -> None:
        self._summary = summary

    def outbox_summary(self, namespace: str) -> dict:
        assert namespace == DEFAULT_NAMESPACE
        return dict(self._summary)


class _RaisingOutbox(InMemoryRecordStore):
    """A record store whose outbox read raises for the named namespaces."""

    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self._failing = failing

    def outbox_summary(self, namespace: str, plan_name=None) -> dict:
        if namespace in self._failing:
            raise RuntimeError(f"secret detail in {namespace}")
        return super().outbox_summary(namespace, plan_name)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    monkeypatch.delenv("PIPELINE_OUTBOX_STALE_SECONDS", raising=False)
    monkeypatch.delenv("PIPELINE_STORE", raising=False)


def _approved(status: str) -> dict:
    return {"approval": {"status": status}}


def _ts(age_s: float) -> str:
    """An ISO-8601 UTC timestamp that is *age_s* seconds old."""
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


def _counts(snapshot) -> dict[str, int]:
    return {labels["status"]: value for labels, value in snapshot["fagan_plans"]}


# -- plan samples -------------------------------------------------------------


def test_plans_in_default_and_team_namespaces_are_counted_by_status(tmp_path):
    records = InMemoryRecordStore()
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, _approved("approved"))
    records.put_manifest(TEAM, TEAM_PLAN, _approved("pending_approval"))
    records.put_manifest(TEAM, "second-team-plan", _approved("approved"))

    snapshot = metrics_exposition.collect(FileHostState(root=tmp_path), _Store(records))

    assert _counts(snapshot) == {
        "approved": 2,
        "pending_approval": 1,
        "rejected": 0,
        "none": 0,
    }


def test_a_default_only_store_yields_the_samples_it_yields_today(tmp_path):
    records = InMemoryRecordStore()
    records.put_manifest(DEFAULT_NAMESPACE, "alpha", _approved("approved"))
    records.put_manifest(DEFAULT_NAMESPACE, "beta", _approved("rejected"))

    snapshot = metrics_exposition.collect(FileHostState(root=tmp_path), _Store(records))

    assert snapshot["fagan_plans"] == [
        ({"status": "approved"}, 1),
        ({"status": "pending_approval"}, 0),
        ({"status": "rejected"}, 1),
        ({"status": "none"}, 0),
    ]


def test_a_store_without_list_namespaces_counts_only_the_default_namespace(tmp_path):
    records = InMemoryRecordStore()
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, _approved("approved"))
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))

    snapshot = metrics_exposition.collect(
        FileHostState(root=tmp_path), _Store(_HiddenListing(records))
    )

    assert _counts(snapshot)["approved"] == 1


def test_a_plan_name_that_fails_the_key_check_is_skipped_in_every_namespace(tmp_path):
    class _ListsAnInvalidName(InMemoryRecordStore):
        def list_plans(self, namespace):
            return sorted({*super().list_plans(namespace), ".."})

    records = _ListsAnInvalidName()
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, _approved("approved"))
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))

    snapshot = metrics_exposition.collect(FileHostState(root=tmp_path), _Store(records))

    assert _counts(snapshot)["approved"] == 2


def test_a_team_namespace_whose_plan_read_raises_is_skipped_not_fatal(tmp_path):
    class _TeamListingRaises(InMemoryRecordStore):
        def get_manifest(self, namespace, plan_name):
            if namespace == TEAM:
                raise RuntimeError("secret detail")
            return super().get_manifest(namespace, plan_name)

    records = _TeamListingRaises()
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, _approved("approved"))
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))

    snapshot = metrics_exposition.collect(FileHostState(root=tmp_path), _Store(records))

    assert _counts(snapshot)["approved"] == 1
    assert [value for _, value in snapshot["fagan_metrics_errors_total"]] == [
        0,
        0,
        0,
        0,
        0,
    ]


# -- outbox -------------------------------------------------------------------


def test_outbox_entries_in_two_namespaces_sum_and_the_older_age_wins():
    records = InMemoryRecordStore()
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))
    records.append_outbox(DEFAULT_NAMESPACE, "alpha", "email", {"ts": _ts(100)})
    records.append_outbox(TEAM, "beta", "email", {"ts": _ts(1200)})
    records.append_outbox(TEAM, "beta", "email", {"ts": _ts(10)})

    summary = metrics_exposition.outbox_summary(_Store(records))

    assert summary["files"] == 2
    assert summary["records"] == 3
    assert summary["oldest_age_s"] == pytest.approx(1200, abs=5)


def test_no_outbox_entries_anywhere_summarise_to_zero():
    records = InMemoryRecordStore()
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))

    summary = metrics_exposition.outbox_summary(_Store(records))

    assert summary == {"files": 0, "records": 0, "oldest_age_s": None}


def test_a_namespace_whose_outbox_read_raises_does_not_stop_the_others(
    tmp_path, caplog
):
    records = _RaisingOutbox(failing={TEAM})
    records.put_manifest(DEFAULT_NAMESPACE, DEFAULT_PLAN, _approved("approved"))
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))
    records.append_outbox(DEFAULT_NAMESPACE, "alpha", "email", {"ts": _ts(30)})

    with caplog.at_level(logging.WARNING):
        snapshot = metrics_exposition.collect(
            FileHostState(root=tmp_path), _Store(records)
        )

    assert snapshot["fagan_outbox_records"] == [({}, 1)]
    skipped = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in r.getMessage() for r in skipped)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "secret detail" not in logged
    assert TEAM not in logged


def test_every_outbox_read_failing_raises_rather_than_reporting_an_empty_outbox():
    records = _RaisingOutbox(failing={DEFAULT_NAMESPACE, TEAM})
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))

    with pytest.raises(RuntimeError):
        metrics_exposition.outbox_summary(_Store(records))


def test_a_stand_in_answering_only_outbox_summary_still_yields_the_default(tmp_path):
    summary = {"files": 1, "records": 4, "oldest_age_s": 60}

    assert metrics_exposition.outbox_summary(_Store(_OutboxOnly(summary))) == summary


def test_health_reflects_a_stale_outbox_that_exists_only_in_a_team_namespace(
    tmp_path,
):
    records = InMemoryRecordStore()
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))
    records.append_outbox(TEAM, TEAM_PLAN, "email", {"ts": _ts(1200)})

    extras = health_freshness.health_extras(
        FileHostState(root=tmp_path), _Store(records)
    )

    assert extras["healthy"] is False
    assert "outbox_stale" in extras["degraded"]
    assert extras["outbox"]["records"] == 1


# -- labels -------------------------------------------------------------------


def test_no_plan_or_outbox_sample_carries_a_namespace_or_plan_name_label(tmp_path):
    records = InMemoryRecordStore()
    records.put_manifest(TEAM, TEAM_PLAN, _approved("approved"))
    records.append_outbox(TEAM, TEAM_PLAN, "email", {"ts": _ts(30)})

    snapshot = metrics_exposition.collect(FileHostState(root=tmp_path), _Store(records))

    for name in (
        "fagan_plans",
        "fagan_outbox_records",
        "fagan_outbox_oldest_age_seconds",
    ):
        for labels, _value in snapshot.get(name, []):
            assert set(labels) <= {"status"}
            assert not any(
                TEAM in str(value) or TEAM_PLAN in str(value)
                for value in labels.values()
            )
