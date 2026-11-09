"""Usage-record contract for every record store (ADR 0012 decision 3).

The usage-record behaviour lives in :mod:`pipeline.store_contract_usage` and is
run here against all three implementations, so a store that passes alone proves
nothing.  The same module holds the two checks that are about the *contract*
rather than one store: the ``RecordStore`` protocol declares no usage operation
but ``append_usage`` and ``get_usage``, and the field set is pinned to the
literal field list in ``docs/TELEMETRY_CONTRACT.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline.store import FileStore
from pipeline.store_contract_usage import UsageContract
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import USAGE_RECORD_FIELDS, RecordStore
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
_CONTRACT_DOC = Path(__file__).resolve().parents[2] / 'docs' / 'TELEMETRY_CONTRACT.md'
_FIELD_SENTENCE = (
    'There is exactly one usage record per model call, with exactly these '
    'fields:'
)
_FIELD_SENTENCE_END = 'The record carries no content'


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    """Point every plans-directory binding at a fresh directory."""
    directory = tmp_path / 'plans'
    directory.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', directory)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', directory)
    return directory


class TestInMemoryRecordStoreUsage(UsageContract):
    """The usage contract, run against the in-memory reference store."""

    def make_store(self):
        return InMemoryRecordStore()


class TestFileRecordsUsage(UsageContract):
    """The usage contract, run against the file-backed record view."""

    def make_store(self):
        return FileStore().records()

    def plans_dir(self):
        return pipeline.server.PLAN_DIR


class TestSqliteRecordsUsage(UsageContract):
    """The usage contract, run against the sqlite-backed record view."""

    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        store = SqliteStore(str(tmp_path / 's.db'))
        self._store = store.records()
        try:
            yield self._store
        finally:
            store.close()

    def make_store(self):
        return self._store

    def plans_dir(self):
        return pipeline.server.PLAN_DIR


class TestUsageRecordContractShape:
    """Checks about the contract itself, not about one implementation."""

    def test_protocol_declares_only_append_usage_and_get_usage(self):
        declared = {
            name
            for name in dir(RecordStore)
            if 'usage' in name.lower() and not name.startswith('_')
        }
        assert declared == {'append_usage', 'get_usage'}

    def test_field_set_is_pinned_to_the_telemetry_contract(self):
        """The docs literal is the source of truth; the code must match it."""
        text = _CONTRACT_DOC.read_text(encoding='utf-8')
        start = text.index(_FIELD_SENTENCE)
        end = text.index(_FIELD_SENTENCE_END, start)
        sentence = text[start:end]
        documented = tuple(re.findall(r'`([^`]+)`', sentence))
        assert documented == USAGE_RECORD_FIELDS
