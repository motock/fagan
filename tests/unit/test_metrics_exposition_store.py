"""``fagan_plans`` counts plans through the record store, not a local glob.

The metrics endpoint counted plans by walking ``*.manifest.json`` in a plan
directory, so a store-backed deployment reported whatever manifests happened to
sit on the scheduler's disk -- a plan the database does not know at all -- and
missed a plan the store knows with no file on this host.

The count now goes through the one store-first read,
:func:`pipeline.manifest_source.read_plan_names` and
:func:`pipeline.manifest_source.read_plan_manifest`, pinned here:

* a plan only the record store knows is counted;
* a non-empty store listing is the answer, so a local file cannot shadow it;
* a store that *raises* is advisory for a scrape (``fallback_on_error=True``):
  the file is counted and ``fagan_metrics_errors_total{source="plans"}`` stays
  0, exactly like ``pipeline.tick_checks``' advisory read;
* two successive scrapes re-read the store -- nothing is cached between calls.

With the legacy :class:`pipeline.store.FileStore` (the unit-test default) the
record view is ``None`` and the file read stays the primary read, so the
pre-existing file-based tests in ``test_metrics_exposition.py`` keep winning.
"""

from __future__ import annotations

import json

import pytest

from pipeline import metrics_exposition as m
from pipeline import server
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.unit._injected_state import host_at


class _Records:
    """A record view answering the two reads the plan count needs."""

    def __init__(self, names=(), manifests=None, *, listing_error=None) -> None:
        self._names = list(names)
        self._manifests = manifests or {}
        self._listing_error = listing_error
        self.listed: list[str] = []
        self.requested: list[tuple[str, str]] = []

    def list_plans(self, namespace: str) -> list[str]:
        self.listed.append(namespace)
        if self._listing_error is not None:
            raise self._listing_error
        return list(self._names)

    def get_manifest(self, namespace: str, plan_name: str):
        self.requested.append((namespace, plan_name))
        return self._manifests.get(plan_name)


class _Store:
    """A store whose only interesting surface is its record view."""

    def __init__(self, records) -> None:
        self._records = records

    def records(self):
        return self._records


def _approved(status: str) -> dict:
    return {"approval": {"status": status}}


def _counts(snapshot, status: str) -> int:
    return {labels["status"]: value for labels, value in snapshot["fagan_plans"]}[status]


def _plans_error_count(snapshot) -> int:
    return {
        labels["source"]: value
        for labels, value in snapshot["fagan_metrics_errors_total"]
    }["plans"]


def _write_manifest(directory, plan_name: str, payload) -> None:
    (directory / f"{plan_name}.manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def quiet_producers(monkeypatch):
    """Silence the other producers: only the plan count is under test."""
    monkeypatch.setattr(m, "health_extras", lambda *_a, **_k: {})
    monkeypatch.setattr(m, "outbox_summary", lambda *_a, **_k: {})
    monkeypatch.setattr(m, "consumer_status", dict)
    monkeypatch.setattr(m, "service_level_percentiles", lambda *_a, **_k: {})


def test_a_store_only_plan_is_counted(monkeypatch, tmp_path) -> None:
    records = _Records(
        names=["alpha"], manifests={"alpha": _approved("approved")}
    )
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(host_at(tmp_path), server._store)

    assert _counts(snapshot, "approved") == 1
    assert _counts(snapshot, "none") == 0
    assert records.listed == [DEFAULT_NAMESPACE]
    assert records.requested == [(DEFAULT_NAMESPACE, "alpha")]
    assert _plans_error_count(snapshot) == 0


def test_the_store_listing_is_not_shadowed_by_a_local_file(
    monkeypatch, tmp_path
) -> None:
    _write_manifest(tmp_path, "beta", _approved("rejected"))
    records = _Records(
        names=["alpha"], manifests={"alpha": _approved("approved")}
    )
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(host_at(tmp_path), server._store)

    assert _counts(snapshot, "approved") == 1
    assert _counts(snapshot, "rejected") == 0


def test_the_store_manifest_wins_over_the_same_plans_local_file(
    monkeypatch, tmp_path
) -> None:
    _write_manifest(tmp_path, "alpha", _approved("rejected"))
    records = _Records(
        names=["alpha"], manifests={"alpha": _approved("approved")}
    )
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(host_at(tmp_path), server._store)

    assert _counts(snapshot, "approved") == 1
    assert _counts(snapshot, "rejected") == 0


def test_a_store_error_is_counted_as_a_plans_error_without_its_text(
    monkeypatch, tmp_path
) -> None:
    """The store is the only plan source now, so a failing store is an error, not a fallback."""
    _write_manifest(tmp_path, "alpha", _approved("approved"))
    records = _Records(listing_error=RuntimeError("store unavailable: secret-dsn"))
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(host_at(tmp_path), server._store)

    assert "fagan_plans" not in snapshot
    assert _plans_error_count(snapshot) == 1
    assert "secret-dsn" not in m.render(snapshot)


def test_two_scrapes_re_read_the_store(monkeypatch, tmp_path) -> None:
    """The count is not cached across calls: call 2 sees the new plan."""
    records = _Records(
        names=["alpha"], manifests={"alpha": _approved("approved")}
    )
    monkeypatch.setattr(server, "_store", _Store(records))

    first = m.collect(host_at(tmp_path), server._store)
    assert _counts(first, "approved") == 1

    records._names = ["alpha", "beta"]
    records._manifests = {
        "alpha": _approved("approved"),
        "beta": _approved("approved"),
    }

    second = m.collect(host_at(tmp_path), server._store)
    assert _counts(second, "approved") == 2
    assert records.listed == [DEFAULT_NAMESPACE, DEFAULT_NAMESPACE]


def test_an_empty_store_listing_counts_zero_plans_never_the_host_files(
    monkeypatch, tmp_path
) -> None:
    """An injected empty store is an answer: the host's manifests are not a second source."""
    _write_manifest(tmp_path, "alpha", _approved("approved"))
    records = _Records(names=[])
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(host_at(tmp_path), server._store)

    assert _counts(snapshot, "approved") == 0

