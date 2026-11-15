"""Tests for the two-segment namespace form (nf-01).

A namespace is either the existing single segment or exactly two segments
joined by one ``/``. Core treats the two-segment form as an opaque key: these
tests pin acceptance, refusal, and the per-store behaviour, never a split.
"""

from __future__ import annotations

import pytest

from pipeline import namespace_names, persistence, server
from pipeline.namespace_names import SEGMENT, is_segmented_namespace
from pipeline.store import FileRecords, FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    validate_namespace,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
MANIFEST = {'schema_version': 1, 'stories': {'S1': {'status': 'pending'}}}
TWO_SEGMENT = 'a/b'

VALID_TWO_SEGMENT = ['a/b', 'acme/core-1', 'x0/y-z', 'a' * 63 + '/b']

INVALID_TWO_SEGMENT = [
    '../x',
    'a//b',
    'a/b/c',
    '/a',
    'a/',
    'a/B',
    'a/b\n',
    'a/' + 'b' * 64,
    'a/' + '..',
    'a\\b',
    'a/_b',
    'a b/c',
    '',
    '/',
]


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """Isolate the on-disk plan projection, as the store tests do."""
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


def test_segment_pattern_is_the_lower_case_dns_label_form():
    assert SEGMENT.fullmatch('a') is not None
    assert SEGMENT.fullmatch('core-1') is not None
    assert SEGMENT.fullmatch('-a') is None
    assert SEGMENT.fullmatch('a-') is None
    assert SEGMENT.fullmatch('A') is None
    assert SEGMENT.fullmatch('a_b') is None
    assert SEGMENT.fullmatch('a' * 64) is None


@pytest.mark.parametrize('value', VALID_TWO_SEGMENT)
def test_is_segmented_namespace_accepts_two_segments(value):
    assert is_segmented_namespace(value) is True


@pytest.mark.parametrize('value', INVALID_TWO_SEGMENT)
def test_is_segmented_namespace_rejects_malformed_values(value):
    assert is_segmented_namespace(value) is False


@pytest.mark.parametrize('value', [None, 5, b'a/b', ['a', 'b'], 'default'])
def test_is_segmented_namespace_rejects_non_strings_and_single_segments(value):
    assert is_segmented_namespace(value) is False


@pytest.mark.parametrize('value', VALID_TWO_SEGMENT)
def test_validate_namespace_accepts_two_segments(value):
    assert validate_namespace(value) == value


def test_validate_namespace_still_accepts_the_single_segment_default():
    assert validate_namespace(DEFAULT_NAMESPACE) == DEFAULT_NAMESPACE
    assert validate_namespace('ns-1') == 'ns-1'


@pytest.mark.parametrize('value', INVALID_TWO_SEGMENT)
def test_validate_namespace_refuses_malformed_two_segment_values(value):
    with pytest.raises(InvalidRecordKeyError, match='^invalid namespace$'):
        validate_namespace(value)


def test_validate_namespace_error_never_echoes_the_value():
    value = '../secret-value/b'
    with pytest.raises(InvalidRecordKeyError) as excinfo:
        validate_namespace(value)
    assert 'secret-value' not in str(excinfo.value)


def test_memory_store_round_trips_a_two_segment_manifest():
    store = InMemoryRecordStore()
    store.put_manifest(TWO_SEGMENT, PLAN, MANIFEST)
    assert store.get_manifest(TWO_SEGMENT, PLAN) == MANIFEST
    assert store.list_plans(TWO_SEGMENT) == [PLAN]


def test_memory_default_namespace_cannot_see_a_two_segment_manifest():
    store = InMemoryRecordStore()
    store.put_manifest(TWO_SEGMENT, PLAN, MANIFEST)
    assert store.get_manifest(DEFAULT_NAMESPACE, PLAN) is None
    assert store.list_plans(DEFAULT_NAMESPACE) == []


def test_sqlite_refuses_a_two_segment_namespace_and_writes_nothing(tmp_path, plan_dir):
    """SqliteStore serves only the default namespace, by design (ADR 0012)."""
    records = SqliteStore(str(tmp_path / 'records.db')).records()
    with pytest.raises(UnknownNamespaceError):
        records.put_manifest(TWO_SEGMENT, PLAN, MANIFEST)
    with pytest.raises(UnknownNamespaceError):
        records.get_manifest(TWO_SEGMENT, PLAN)
    assert records.list_plans(DEFAULT_NAMESPACE) == []


def test_sqlite_refuses_a_malformed_two_segment_namespace_before_storage(
    tmp_path, plan_dir
):
    records = SqliteStore(str(tmp_path / 'records.db')).records()
    with pytest.raises(InvalidRecordKeyError):
        records.put_manifest('../x', PLAN, MANIFEST)
    assert records.list_plans(DEFAULT_NAMESPACE) == []


def test_file_store_refuses_a_two_segment_namespace_and_writes_nothing(
    tmp_path, plan_dir
):
    records = FileStore().records()
    assert isinstance(records, FileRecords)
    with pytest.raises(UnknownNamespaceError):
        records.put_manifest(TWO_SEGMENT, PLAN, MANIFEST)
    with pytest.raises(UnknownNamespaceError):
        records.get_manifest(TWO_SEGMENT, PLAN)
    assert list(plan_dir.iterdir()) == []
    assert records.list_plans(DEFAULT_NAMESPACE) == []


def test_default_namespace_behaviour_is_unchanged_without_a_plugin():
    store = InMemoryRecordStore()
    store.put_manifest(DEFAULT_NAMESPACE, PLAN, MANIFEST)
    assert store.get_manifest(DEFAULT_NAMESPACE, PLAN) == MANIFEST
    assert store.list_plans(DEFAULT_NAMESPACE) == [PLAN]
    with pytest.raises(InvalidRecordKeyError):
        store.put_manifest('Default', PLAN, MANIFEST)


def test_namespace_names_module_has_no_core_splitting_helper():
    """Core must not parse the parts: only a membership test is exported."""
    public = {name for name in dir(namespace_names) if not name.startswith('_')}
    assert public <= {'SEGMENT', 'is_segmented_namespace', 're', 'annotations'}
