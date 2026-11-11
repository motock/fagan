"""Contract tests for the plan-completion records (ADR 0012 decision 3).

Plan completion leaves two durable artefacts behind: the once-only
``<plan>.plan_completed`` marker and the ``<plan>.report.md`` summary.  Both
become *records* of the namespaced store, so every backend answers the same
four questions:

* ``mark_plan_completed`` -- ``True`` only for the call that created the
  marker, ``False`` for every later call (the marker is created atomically);
* ``is_plan_completed`` -- whether the marker exists;
* ``put_plan_report`` -- store the summary, replacing the previous one
  wholesale;
* ``get_plan_report`` -- the stored summary, or ``None`` when there is none.

The same behaviour is asserted against the three record-store
implementations: the file-backed store, the sqlite store and the in-memory
reference store.
"""

from __future__ import annotations

import pytest

from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-completion'
OTHER_PLAN = 'other-plan'
REPORT = 'SUMMARY[plan-completion]\n'

MARK_METHODS = ('mark_plan_completed', 'is_plan_completed')
REPORT_METHODS = ('put_plan_report', 'get_plan_report')


@pytest.fixture(params=('file', 'sqlite', 'memory'))
def backend(request):
    """The implementation under test, one per run of every test below.

    The backends are run one at a time, not side by side in one directory: a
    plan-completion marker is *once-only*, so the second backend to look at a
    shared plans directory would find the first backend's marker and never
    report the ``True`` that creation must return.  Giving each backend its own
    directory keeps the assertion (first mark ``True``, second ``False``) about
    the implementation and not about test ordering.
    """
    return request.param


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory wired into both module-level bindings."""
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


@pytest.fixture
def stores(plan_dir, tmp_path, backend):
    """The implementation under test, keyed by backend name."""
    if backend == 'memory':
        yield {'memory': InMemoryRecordStore()}
        return
    if backend == 'file':
        yield {'file': FileStore().records()}
        return
    sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
    try:
        yield {'sqlite': sqlite_store.records()}
    finally:
        close = getattr(sqlite_store, 'close', None)
        if close is not None:
            close()


def _all_stores(stores):
    """Yield ``(backend, store)`` for every implementation under test."""
    yield from stores.items()


# -- the contract itself ------------------------------------------------


def test_record_store_methods_contain_the_plan_completion_names():
    """The four plan-completion operations are part of the closed contract."""
    from pipeline import store_records

    methods = store_records.RECORD_STORE_METHODS
    for name in MARK_METHODS + REPORT_METHODS:
        assert name in methods, f'{name} must be in RECORD_STORE_METHODS'


def test_the_record_store_protocol_declares_the_plan_completion_names():
    """The Protocol declares the same four names as the method tuple."""
    from pipeline import store_records

    protocol = store_records.RecordStore
    for name in MARK_METHODS + REPORT_METHODS:
        assert hasattr(protocol, name), f'RecordStore must declare {name}'


# -- the marker ---------------------------------------------------------


def test_is_plan_completed_is_false_before_the_marker_is_marked(stores):
    for backend, store in _all_stores(stores):
        assert store.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is False, backend


def test_mark_plan_completed_returns_true_for_the_call_that_created_it(stores):
    for backend, store in _all_stores(stores):
        assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is True, backend


def test_mark_plan_completed_twice_is_false_the_second_time(stores):
    for backend, store in _all_stores(stores):
        assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is True, backend
        assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is False, backend


def test_is_plan_completed_is_true_after_the_marker_is_marked(stores):
    for backend, store in _all_stores(stores):
        store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)
        assert store.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True, backend


def test_the_marker_is_per_plan(stores):
    for backend, store in _all_stores(stores):
        store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)
        assert store.is_plan_completed(DEFAULT_NAMESPACE, OTHER_PLAN) is False, backend
        assert store.mark_plan_completed(DEFAULT_NAMESPACE, OTHER_PLAN) is True, backend


# -- the report ---------------------------------------------------------


def test_get_plan_report_of_a_missing_report_is_none(stores):
    for backend, store in _all_stores(stores):
        assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) is None, backend


def test_put_then_get_plan_report_returns_the_text(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)
        assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) == REPORT, backend


def test_put_plan_report_replaces_the_previous_report_wholesale(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_report(DEFAULT_NAMESPACE, PLAN, 'first report\n')
        store.put_plan_report(DEFAULT_NAMESPACE, PLAN, 'second report\n')
        assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) == 'second report\n', backend


def test_the_report_is_per_plan(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_report(DEFAULT_NAMESPACE, PLAN, 'mine\n')
        assert store.get_plan_report(DEFAULT_NAMESPACE, OTHER_PLAN) is None, backend


def test_an_empty_report_round_trips(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_report(DEFAULT_NAMESPACE, PLAN, '')
        assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) == '', backend


# -- rejected keys and namespaces --------------------------------------


def test_an_invalid_plan_name_is_refused_by_the_marker_operations(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.mark_plan_completed(DEFAULT_NAMESPACE, '../escape')
        with pytest.raises(InvalidRecordKeyError):
            store.is_plan_completed(DEFAULT_NAMESPACE, '../escape')


def test_an_invalid_plan_name_is_refused_by_the_report_operations(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.put_plan_report(DEFAULT_NAMESPACE, '../escape', 'x')
        with pytest.raises(InvalidRecordKeyError):
            store.get_plan_report(DEFAULT_NAMESPACE, '../escape')


def test_a_refused_plan_name_writes_nothing_to_the_plans_directory(stores, plan_dir):
    for _backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.mark_plan_completed(DEFAULT_NAMESPACE, '../escape')
        with pytest.raises(InvalidRecordKeyError):
            store.put_plan_report(DEFAULT_NAMESPACE, '../escape', 'x')
    assert list(plan_dir.iterdir()) == []


def test_the_marker_operations_refuse_an_unknown_namespace(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.mark_plan_completed('other-namespace', PLAN)
        with pytest.raises(UnknownNamespaceError):
            store.is_plan_completed('other-namespace', PLAN)


def test_the_report_operations_refuse_an_unknown_namespace(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.put_plan_report('other-namespace', PLAN, 'x')
        with pytest.raises(UnknownNamespaceError):
            store.get_plan_report('other-namespace', PLAN)


def test_an_unknown_namespace_writes_nothing_to_the_plans_directory(stores, plan_dir):
    for _backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.mark_plan_completed('other-namespace', PLAN)
        with pytest.raises(UnknownNamespaceError):
            store.put_plan_report('other-namespace', PLAN, 'x')
    assert list(plan_dir.iterdir()) == []


# -- rejected payloads --------------------------------------------------


@pytest.mark.parametrize('payload', [None, 5, ['a'], b'bytes', {'a': 1}])
def test_put_plan_report_of_a_non_str_report_raises_type_error(stores, payload):
    for backend, store in _all_stores(stores):
        with pytest.raises(TypeError):
            store.put_plan_report(DEFAULT_NAMESPACE, PLAN, payload)
        assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) is None, backend


def test_a_rejected_report_writes_nothing_to_the_plans_directory(stores, plan_dir):
    for _backend, store in _all_stores(stores):
        with pytest.raises(TypeError):
            store.put_plan_report(DEFAULT_NAMESPACE, PLAN, 5)
    assert list(plan_dir.iterdir()) == []


# -- the sqlite backend keeps the completion records in its database -----


def test_the_sqlite_backend_keeps_the_marker_and_the_report_in_its_database(
    stores, plan_dir, backend
):
    """The sqlite store's completion records are rows, so no file is written.

    The file backend still keeps today's layout; the sqlite backend moved the
    marker and the report into its own database, so the plans directory stays
    empty.
    """
    if backend != 'sqlite':
        pytest.skip('the database layout is the sqlite store\'s own')
    store = stores['sqlite']

    assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
    store.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)

    assert store.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
    assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) == REPORT
    assert list(plan_dir.iterdir()) == []
