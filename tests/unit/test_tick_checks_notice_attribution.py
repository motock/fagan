"""Platform notices are attributed to the first plan the *record store* knows.

``pipeline.tick_checks`` labels its consumer-watchdog and outbox alerts with a
plan name only because ``persistence._notify_user`` needs a routing key: the
alert is operator-audience, not that plan's (see enterprise ADR 0001). Naming a
plan is the stopgap, so the plan must at least be the one the deployment
*records*. The tick used to glob ``paths.PLAN_DIR`` for ``*.manifest.json``
directly, which on a store-backed deployment names a plan the scheduler's disk
happens to carry - possibly one the database does not know at all.

The read now mirrors :func:`pipeline.manifest_source.read_plan_manifest`: the
store's record view first, this host's plan directory second. With the legacy
:class:`pipeline.store.FileStore` (the unit-test default) its record view reads
the same JSON files through a binding of its own, so the file glob *is* the
primary read - pinned below so the fixture binding keeps winning.
"""

from __future__ import annotations

import pytest

from pipeline import paths, persistence, tick_checks
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE

#: One stale outbox, as ``outbox_health.outbox_summary`` reports it.
STALE = {"records": 1, "files": 1, "oldest_age_s": 1200}


class _Records:
    """A record view that answers ``list_plans`` - or raises, on demand."""

    def __init__(self, names=(), *, error: Exception | None = None) -> None:
        self._names = list(names)
        self._error = error
        self.calls: list[str] = []

    def list_plans(self, namespace: str) -> list[str]:
        self.calls.append(namespace)
        if self._error is not None:
            raise self._error
        return list(self._names)


class _Store:
    """A store whose only interesting behaviour is its record view."""

    def __init__(self, records) -> None:
        self._records = records

    def records(self):
        return self._records


class _UnlistableStore:
    """A store with no usable record view at all."""

    def records(self):
        raise RuntimeError("no record view here")


@pytest.fixture()
def notices(monkeypatch):
    """Capture ``persistence._notify_user`` calls as ``(plan, event)`` pairs."""
    seen: list[tuple[str, str | None]] = []

    def fake_notify(plan, message, **kwargs):
        seen.append((plan, kwargs.get("event")))

    monkeypatch.setattr(persistence, "_notify_user", fake_notify)
    return seen


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """A plan directory with one local manifest, bound where the tick reads it."""
    (tmp_path / "a-local.manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    return tmp_path


def _use_store(monkeypatch, store) -> None:
    from pipeline import server

    monkeypatch.setattr(server, "_store", store, raising=False)


def _notify_stale(summary: dict | None = None) -> None:
    tick_checks._notify_outbox("outbox_stale", summary or dict(STALE))


def test_outbox_notice_is_attributed_to_the_first_recorded_plan(
    plan_dir, notices, monkeypatch
):
    """The store wins over a local manifest that sorts first."""
    records = _Records(["zeta", "beta"])
    _use_store(monkeypatch, _Store(records))

    _notify_stale({"records": 2, "files": 1, "oldest_age_s": 1200})

    assert notices == [("beta", "outbox_stale")]
    assert records.calls == [DEFAULT_NAMESPACE]


def test_consumer_notice_is_attributed_to_the_first_recorded_plan(
    plan_dir, notices, monkeypatch
):
    """The watchdog's notice routes through the same read."""
    records = _Records(["zeta", "beta"])
    _use_store(monkeypatch, _Store(records))

    tick_checks._notify_transition("durable-1", "consumer_stalled")

    assert notices == [("beta", "consumer_stalled")]
    assert records.calls == [DEFAULT_NAMESPACE]


def test_recovery_notice_uses_the_same_attribution(plan_dir, notices, monkeypatch):
    _use_store(monkeypatch, _Store(_Records(["beta"])))

    tick_checks._notify_outbox("outbox_recovered", dict(STALE))

    assert notices == [("beta", "outbox_recovered")]


def test_file_store_falls_back_to_the_bound_plan_dir(plan_dir, notices, monkeypatch):
    """``FileStore``'s record view re-reads the same files, so it is no source.

    If it were consulted it would glob ``pipeline.store.PLAN_DIR`` - the real
    plans directory - and the test's fixture binding would be bypassed.
    """
    _use_store(monkeypatch, FileStore())

    _notify_stale()

    assert notices == [("a-local", "outbox_stale")]


def test_a_store_that_cannot_list_plans_degrades_to_the_file(
    plan_dir, notices, monkeypatch
):
    """Advisory: a broken store must not take the tick's notice down."""
    _use_store(monkeypatch, _Store(_Records(error=RuntimeError("db is down"))))

    _notify_stale()

    assert notices == [("a-local", "outbox_stale")]


def test_a_store_without_a_record_view_degrades_to_the_file(
    plan_dir, notices, monkeypatch
):
    _use_store(monkeypatch, _UnlistableStore())

    _notify_stale()

    assert notices == [("a-local", "outbox_stale")]


def test_no_plan_anywhere_means_no_notice(tmp_path, notices, monkeypatch):
    """Nothing to attribute to yet: skip rather than invent a routing key."""
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _use_store(monkeypatch, _Store(_Records([])))

    _notify_stale()
    tick_checks._notify_transition("durable-1", "consumer_stalled")

    assert notices == []


def test_empty_store_listing_falls_back_to_local_manifests(
    plan_dir, notices, monkeypatch
):
    """A store that records no plans does not hide the local ones."""
    _use_store(monkeypatch, _Store(_Records([])))

    _notify_stale()

    assert notices == [("a-local", "outbox_stale")]
