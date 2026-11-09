"""``SqliteStore``'s namespaced record view (ADR 0012 stage A).

``store.records()`` returns a :class:`pipeline.store_records.RecordStore` over
the same database. Stage A serves the single namespace ``'default'`` only, and
every record operation is a view of a legacy ``SqliteStore`` call, so the two
APIs must never disagree about what is stored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import pipeline as pipeline_package
import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import notification_outbox, paths
from pipeline.store_contract import RecordStoreContract, SingleNamespaceContract
from pipeline.store_contract_transactions import TransactionContract
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError
from pipeline.store_sqlite import SqliteStore
from tests.unit._store_outbox_helpers import SingleNamespaceOutboxBehaviour

PLAN = 'plan-a'


@pytest.fixture()
def db_path(tmp_path):
    """The absolute database path every store in one test shares."""
    return str(tmp_path / 's.db')


@pytest.fixture()
def plan_dir(db_path, monkeypatch):
    """Isolate ``PLAN_DIR`` exactly as ``test_store_sqlite`` does.

    ``SqliteStore`` keeps each manifest's on-disk projection under ``PLAN_DIR``;
    without this a test would write into the operator's real plans directory.
    """
    directory = db_path + '.plans'
    Path(directory).mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', Path(directory))
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', Path(directory))
    return Path(directory)


def _row_count(db_path: str) -> int:
    """Count ``story_manifests`` rows, out of band from the store."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute('SELECT count(*) FROM story_manifests').fetchone()[0]
    finally:
        conn.close()


class TestSqliteRecordsTransaction(TransactionContract):
    """The transaction lock contract, run against the sqlite-backed store."""

    @pytest.fixture(autouse=True)
    def _store(self, db_path, plan_dir):
        """A ``records()`` view on a fresh database, closed after the test."""
        store = SqliteStore(db_path)
        self._store = store.records()
        try:
            yield self._store
        finally:
            close = getattr(store, 'close', None)
            if close is not None:
                close()

    def make_store(self):
        return self._store


class TestSqliteRecords(RecordStoreContract, SingleNamespaceContract):
    """The record contract, exercised against a real database."""

    @pytest.fixture(autouse=True)
    def _store(self, db_path, plan_dir):
        """A ``records()`` view on a fresh database, closed after the test."""
        store = SqliteStore(db_path)
        self._legacy_store = store
        self._db_path = db_path
        self._store = store.records()
        try:
            yield self._store
        finally:
            close = getattr(store, 'close', None)
            if close is not None:
                close()

    def make_store(self):
        """Return the ``records()`` view built for this test."""
        return self._store

    def test_manifest_written_through_records_is_read_by_the_legacy_store(self):
        manifest = {'title': PLAN, 'steps': [{'id': 1}]}
        self._store.put_manifest(DEFAULT_NAMESPACE, PLAN, manifest)
        assert self._legacy_store.get_manifest(PLAN) == manifest

    def test_manifest_written_through_the_legacy_store_is_read_by_records(self):
        manifest = {'title': PLAN, 'steps': [{'id': 2}]}
        self._legacy_store.save_manifest(PLAN, manifest)
        assert self._store.get_manifest(DEFAULT_NAMESPACE, PLAN) == manifest

    def test_refused_put_manifest_leaves_the_manifests_table_unchanged(self):
        self._store.put_manifest(DEFAULT_NAMESPACE, PLAN, {'v': 1})
        assert _row_count(self._db_path) == 1
        with pytest.raises(UnknownNamespaceError):
            self._store.put_manifest('ops', PLAN, {'v': 2})
        assert _row_count(self._db_path) == 1
        assert self._legacy_store.get_manifest(PLAN) == {'v': 1}
        self._store.put_manifest(DEFAULT_NAMESPACE, PLAN, {'v': 3})
        assert _row_count(self._db_path) == 1
        assert self._legacy_store.get_manifest(PLAN) == {'v': 3}


class TestSqliteRecordsOutbox(SingleNamespaceOutboxBehaviour):
    """The outbox contract, run against the sqlite-backed store.

    The outbox queues are delegated to the file store view for now, so the
    queues live in the plan directory; the plan directory is pointed at a
    throwaway one so nothing lands in the operator's real plans directory.
    """

    @pytest.fixture(autouse=True)
    def _outbox_dirs(self, plan_dir, monkeypatch):
        """Point the two outbox path bindings at the throwaway plan dir."""
        monkeypatch.setattr(paths, 'PLAN_DIR', plan_dir)
        monkeypatch.setattr(notification_outbox, 'PLAN_DIR', plan_dir)
        # See tests/unit/test_store_file_records.py: the outbox path
        # resolution reads ``paths`` as a package attribute.
        monkeypatch.setattr(pipeline_package, 'paths', paths)

    @pytest.fixture(autouse=True)
    def _store(self, db_path, plan_dir):
        """A ``records()`` view on a fresh database, closed after the test."""
        store = SqliteStore(db_path)
        self._store = store.records()
        try:
            yield self._store
        finally:
            close = getattr(store, 'close', None)
            if close is not None:
                close()

    def make_store(self):
        return self._store
