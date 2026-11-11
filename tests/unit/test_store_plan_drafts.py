"""Contract tests for the transient plan draft records (ADR 0012 decision 3).

The plan draft is the *staged* plan document an operator hands to the
pipeline before ingest.  It is a transient record: the store keeps it under
``PLAN_DIR/<plan>.json`` -- the same layout ingest already reads -- so the
draft can be handed to ``save_plan`` and later consumed.

These tests run the same behaviour against the three record-store
implementations: the file-backed store, the sqlite store and the in-memory
reference store.
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    RecordStoreError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
DRAFT = {'epics': [{'summary': 'e', 'stories': [{'key': 'S-1'}]}]}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory wired into both module-level bindings."""
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


@pytest.fixture
def stores(plan_dir, tmp_path):
    """The three implementations under test, keyed by backend name.

    The sqlite store is closed again at the end of the test.
    """
    sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
    try:
        yield {
            'file': FileStore().records(),
            'sqlite': sqlite_store.records(),
            'memory': InMemoryRecordStore(),
        }
    finally:
        close = getattr(sqlite_store, 'close', None)
        if close is not None:
            close()


def _all_stores(stores):
    """Yield ``(backend, store)`` for every implementation under test."""
    yield from stores.items()


# -- the contract itself ------------------------------------------------


def test_record_store_methods_contain_the_four_plan_draft_names():
    """The four draft operations are part of the closed record contract."""
    from pipeline import store_records

    methods = store_records.RECORD_STORE_METHODS
    for name in (
        'put_plan_draft',
        'get_plan_draft',
        'delete_plan_draft',
        'list_plan_drafts',
    ):
        assert name in methods, f'{name} must be in RECORD_STORE_METHODS'


def test_the_record_store_protocol_declares_the_four_plan_draft_names():
    """The Protocol declares the same four names as the method tuple."""
    from pipeline import store_records

    protocol = store_records.RecordStore
    for name in (
        'put_plan_draft',
        'get_plan_draft',
        'delete_plan_draft',
        'list_plan_drafts',
    ):
        assert hasattr(protocol, name), f'RecordStore must declare {name}'


def test_the_record_store_module_exposes_an_unreadable_record_error():
    """A malformed draft raises a typed store error, never a JSON error."""
    from pipeline import store_records

    error = getattr(store_records, 'UnreadableRecordError', None)
    assert error is not None, 'store_records.UnreadableRecordError must exist'
    assert issubclass(error, store_records.RecordStoreError)


# -- round trip ---------------------------------------------------------


def test_put_then_get_plan_draft_returns_the_document(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
        assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT, backend


def test_get_plan_draft_of_a_missing_draft_is_none(stores):
    for backend, store in _all_stores(stores):
        assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None, backend


def test_delete_plan_draft_of_a_missing_draft_is_false_and_raises_nothing(stores):
    for backend, store in _all_stores(stores):
        assert store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is False, backend


def test_delete_then_get_plan_draft_is_none(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
        assert store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is True, backend
        assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None, backend


def test_delete_plan_draft_twice_is_false_the_second_time(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
        assert store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is True
        assert store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is False, backend


def test_put_plan_draft_replaces_the_previous_document_wholesale(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, {'epics': [], 'stale': True})
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, {'epics': [{'summary': 'new'}]})
        stored = store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
        assert stored == {'epics': [{'summary': 'new'}]}, backend
        assert 'stale' not in stored, backend


def test_a_mutated_returned_draft_does_not_change_the_stored_document(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
        document = store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
        document['epics'].append({'summary': 'injected'})
        assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT, backend


def test_list_plan_drafts_of_an_empty_namespace_is_empty(stores):
    for backend, store in _all_stores(stores):
        assert store.list_plan_drafts(DEFAULT_NAMESPACE) == [], backend


def test_list_plan_drafts_returns_the_sorted_plan_names(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, 'zeta', DRAFT)
        store.put_plan_draft(DEFAULT_NAMESPACE, 'alpha', DRAFT)
        store.put_plan_draft(DEFAULT_NAMESPACE, 'mid', DRAFT)
        assert store.list_plan_drafts(DEFAULT_NAMESPACE) == ['alpha', 'mid', 'zeta'], backend


def test_list_plan_drafts_ignores_a_deleted_draft(stores):
    for backend, store in _all_stores(stores):
        store.put_plan_draft(DEFAULT_NAMESPACE, 'alpha', DRAFT)
        store.put_plan_draft(DEFAULT_NAMESPACE, 'beta', DRAFT)
        store.delete_plan_draft(DEFAULT_NAMESPACE, 'alpha')
        assert store.list_plan_drafts(DEFAULT_NAMESPACE) == ['beta'], backend


# -- rejected keys ------------------------------------------------------


@pytest.mark.parametrize('bad_name', ['../x', '', 'a/b'])
def test_put_plan_draft_of_an_invalid_plan_name_raises(stores, bad_name):
    for _backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.put_plan_draft(DEFAULT_NAMESPACE, bad_name, DRAFT)


@pytest.mark.parametrize('bad_name', ['../x', '', 'a/b'])
def test_get_plan_draft_of_an_invalid_plan_name_raises(stores, bad_name):
    for backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.get_plan_draft(DEFAULT_NAMESPACE, bad_name)


@pytest.mark.parametrize('bad_name', ['../x', '', 'a/b'])
def test_delete_plan_draft_of_an_invalid_plan_name_raises(stores, bad_name):
    for backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.delete_plan_draft(DEFAULT_NAMESPACE, bad_name)


def test_a_rejected_plan_name_writes_nothing_to_the_plans_directory(stores, plan_dir):
    for _backend, store in _all_stores(stores):
        with pytest.raises(InvalidRecordKeyError):
            store.put_plan_draft(DEFAULT_NAMESPACE, '../x', DRAFT)
    assert list(plan_dir.iterdir()) == []


def test_put_plan_draft_in_an_unknown_namespace_raises_before_storage(stores, plan_dir):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.put_plan_draft('other-namespace', PLAN, DRAFT)
    assert list(plan_dir.iterdir()) == []


def test_get_plan_draft_in_an_unknown_namespace_raises(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.get_plan_draft('other-namespace', PLAN)


def test_delete_plan_draft_in_an_unknown_namespace_raises(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.delete_plan_draft('other-namespace', PLAN)


def test_list_plan_drafts_in_an_unknown_namespace_raises(stores):
    for backend, store in _all_stores(stores):
        with pytest.raises(UnknownNamespaceError):
            store.list_plan_drafts('other-namespace')


# -- rejected payloads --------------------------------------------------


@pytest.mark.parametrize('payload', ['not-a-dict', 5, None, ['a']])
def test_put_plan_draft_of_a_non_dict_plan_raises_type_error(stores, payload):
    for backend, store in _all_stores(stores):
        with pytest.raises(TypeError):
            store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, payload)
        assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None, backend


def test_a_rejected_plan_payload_writes_nothing_to_the_plans_directory(stores, plan_dir):
    for _backend, store in _all_stores(stores):
        with pytest.raises(TypeError):
            store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, 'not-a-dict')
    assert list(plan_dir.iterdir()) == []


# -- unreadable drafts on disk -----------------------------------------


def test_get_plan_draft_of_a_malformed_draft_raises_a_typed_store_error(stores, plan_dir):
    from pipeline import store_records

    error = store_records.UnreadableRecordError
    (plan_dir / f'{PLAN}.json').write_text('{not json', encoding='utf-8')
    for backend, store in _all_stores(stores):
        with pytest.raises(error) as excinfo:
            store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
        assert isinstance(excinfo.value, RecordStoreError), backend


def test_the_unreadable_draft_error_is_not_a_json_decode_error(stores, plan_dir):
    from pipeline import store_records

    (plan_dir / f'{PLAN}.json').write_text('{not json', encoding='utf-8')
    for backend, store in _all_stores(stores):
        try:
            store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
        except json.JSONDecodeError:  # pragma: no cover - the failure path
            raise AssertionError(
                f'{backend} raised JSONDecodeError instead of a typed store error'
            )
        except store_records.UnreadableRecordError:
            pass
        else:  # pragma: no cover - the failure path
            raise AssertionError(f'{backend} did not raise for a malformed draft')


def test_the_unreadable_draft_error_carries_the_legacy_reason_text(stores, plan_dir):
    """The reason text is the one ``persistence.read_plan_file`` produces."""
    from pipeline import store_records

    (plan_dir / f'{PLAN}.json').write_text('{not json', encoding='utf-8')
    _plan, refusal = persistence.read_plan_file(plan_dir / f'{PLAN}.json')
    assert refusal is not None
    expected = refusal['error']
    for backend, store in _all_stores(stores):
        with pytest.raises(store_records.UnreadableRecordError) as excinfo:
            store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
        assert expected in str(excinfo.value), backend


def test_get_plan_draft_of_a_draft_holding_a_non_object_raises_a_typed_error(
    stores, plan_dir
):
    from pipeline import store_records

    (plan_dir / f'{PLAN}.json').write_text('[1, 2, 3]', encoding='utf-8')
    for backend, store in _all_stores(stores):
        with pytest.raises(store_records.UnreadableRecordError):
            store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)


# -- the file layout ----------------------------------------------------


def test_the_file_store_keeps_the_draft_at_plan_dir_json(stores, plan_dir):
    store = stores['file']
    store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
    written = plan_dir / f'{PLAN}.json'
    assert written.is_file()
    assert json.loads(written.read_text(encoding='utf-8')) == DRAFT


def test_the_file_store_reads_a_hand_written_plan_file(stores, plan_dir):
    """A draft written by hand, the way the pipeline's own fixtures do it."""
    (plan_dir / f'{PLAN}.json').write_text(
        json.dumps({'epics': [{'summary': 'hand'}]}), encoding='utf-8'
    )
    assert stores['file'].get_plan_draft(DEFAULT_NAMESPACE, PLAN) == {
        'epics': [{'summary': 'hand'}]
    }


def test_list_plan_drafts_skips_manifest_decision_and_journal_files(stores, plan_dir):
    """Only ``<plan>.json`` files whose stem holds no '.' are drafts."""
    store = stores['file']
    store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
    (plan_dir / f'{PLAN}.manifest.json').write_text('{}', encoding='utf-8')
    (plan_dir / f'{PLAN}.decisions.json').write_text('[]', encoding='utf-8')
    (plan_dir / f'{PLAN}.S-1.journal.json').write_text('[]', encoding='utf-8')
    assert store.list_plan_drafts(DEFAULT_NAMESPACE) == [PLAN]


def test_list_plan_drafts_skips_a_json_file_without_an_epics_key(stores, plan_dir):
    """A JSON object without ``epics`` is not an ingestable plan."""
    store = stores['file']
    store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
    (plan_dir / 'other.json').write_text(json.dumps({'title': 'x'}), encoding='utf-8')
    assert store.list_plan_drafts(DEFAULT_NAMESPACE) == [PLAN]


# -- the sqlite backend keeps the draft in its database -----------------


def test_the_sqlite_backend_stages_the_draft_in_its_database_not_on_disk(
    stores, plan_dir
):
    """The sqlite store's draft is a row, so the plans directory stays empty.

    The file backend still keeps today's layout; the sqlite backend moved the
    draft into its own database, so a draft it staged leaves no file behind.
    """
    store = stores['sqlite']

    store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT
    assert list(plan_dir.iterdir()) == []
