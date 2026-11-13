"""Tests for the ``FileStore`` namespaced record view (ADR 0012, stage A)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import pipeline as pipeline_package
from pipeline import paths, persistence, server
from pipeline.store import FileRecords, FileStore
from pipeline.store_contract import RecordStoreContract, SingleNamespaceContract
from pipeline.store_contract_transactions import TransactionContract
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.unit._store_outbox_helpers import SingleNamespaceOutboxBehaviour


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


class TestFileRecordsTransaction(TransactionContract):
    """The transaction lock contract, run against the file-backed store."""

    def make_store(self):
        return FileStore().records()

    def plans_dir(self):
        # The file store locks by creating ``<plan>.lock`` in PLAN_DIR, so the
        # contract can prove a rejected key never reaches the lock.
        return server.PLAN_DIR


class TestFileRecords(RecordStoreContract, SingleNamespaceContract):
    def make_store(self):
        return FileStore().records()

    # -- the two views share one record --------------------------------
    def test_manifest_written_through_records_is_readable_by_legacy_store(
        self, plan_dir
    ):
        manifest = {'title': 'p', 'stories': {}}
        FileStore().records().put_manifest(DEFAULT_NAMESPACE, 'p', manifest)
        assert FileStore().get_manifest('p') == manifest

    def test_manifest_written_through_legacy_store_is_readable_through_records(
        self, plan_dir
    ):
        manifest = {'title': 'p', 'stories': {}}
        FileStore().save_manifest('p', manifest)
        assert FileStore().records().get_manifest(DEFAULT_NAMESPACE, 'p') == manifest

    def test_records_returns_a_file_records_view(self, plan_dir):
        assert isinstance(FileStore().records(), FileRecords)

    # -- rejected writes leave no temporary file behind -----------------
    def test_put_manifest_of_a_non_dict_manifest_raises_type_error(self, plan_dir):
        with pytest.raises(TypeError):
            FileStore().records().put_manifest(DEFAULT_NAMESPACE, 'p', ['not', 'a', 'dict'])

    def test_non_json_manifest_leaves_no_tmp_file_behind(self, plan_dir):
        with pytest.raises(TypeError):
            FileStore().records().put_manifest(
                DEFAULT_NAMESPACE, 'p', {'bad': {1, 2, 3}}
            )
        assert list(plan_dir.glob('*.tmp*')) == []

    def test_append_decision_of_a_non_dict_record_raises_type_error(self, plan_dir):
        with pytest.raises(TypeError):
            FileStore().records().append_decision(DEFAULT_NAMESPACE, 'p', 'nope')

    def test_append_journal_of_a_non_dict_record_raises_type_error(self, plan_dir):
        with pytest.raises(TypeError):
            FileStore().records().append_journal(
                DEFAULT_NAMESPACE, 'p', 'STORY-1', 'nope'
            )


LEGACY = '_legacy'
INSTANCE = 'email-a'
PLAN = 'plan-a'


class TestFileRecordsOutbox(SingleNamespaceOutboxBehaviour):
    """The outbox contract, run against the file-backed store."""

    def make_store(self):
        return FileStore().records()


@pytest.fixture()
def outbox_dirs(tmp_path, monkeypatch):
    """Point the two outbox path bindings at separate throwaway directories.

    The legacy queue resolves through ``persistence.PLAN_DIR`` (the binding
    the legacy spool writes through) and an instance queue through
    ``paths.PLAN_DIR`` (the binding the channel writer resolves).  They are
    patched apart so a test can tell which binding a queue follows.
    """
    legacy_dir = tmp_path / 'legacy'
    instance_dir = tmp_path / 'instance'
    legacy_dir.mkdir()
    instance_dir.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", legacy_dir)
    monkeypatch.setattr(paths, 'PLAN_DIR', instance_dir)
    # ``pipeline.persistence._outbox_dirs`` resolves ``paths`` as a PACKAGE
    # ATTRIBUTE (``from . import paths``), while this fixture patches the module
    # object it imported. Bind the package attribute to that same module for
    # the test, so a stray ``pipeline.paths`` re-import elsewhere in the worker
    # cannot leave the code reading a different module's PLAN_DIR.
    monkeypatch.setattr(pipeline_package, 'paths', paths)
    return legacy_dir, instance_dir


class TestFileRecordsOutboxFiles:
    """The on-disk behaviour of the file-backed outbox."""

    @pytest.fixture(autouse=True)
    def _dirs(self, outbox_dirs):
        self.legacy_dir, self.instance_dir = outbox_dirs

    def _store(self):
        return FileStore().records()

    # -- the on-disk layout -------------------------------------------
    def test_the_legacy_queue_uses_the_legacy_spool_file_name(self):
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'event': 'x'})
        assert (self.legacy_dir / f'{PLAN}.outbox.jsonl').exists()

    def test_an_instance_queue_uses_its_own_spool_file(self):
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
        assert (self.instance_dir / f'{PLAN}.outbox.{INSTANCE}.jsonl').exists()

    def test_the_legacy_queue_follows_the_legacy_spool_binding(self):
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'event': 'x'})
        assert list(self.legacy_dir.iterdir())
        assert list(self.instance_dir.iterdir()) == []

    def test_an_instance_queue_follows_the_instance_binding(self):
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
        assert list(self.instance_dir.iterdir())
        assert list(self.legacy_dir.iterdir()) == []

    # -- the bytes on disk ---------------------------------------------
    def test_the_appended_line_is_the_dumped_record_byte_for_byte(self):
        record = {'event': 'plan_completed', 'n': 1}
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, record)
        raw = (self.legacy_dir / f'{PLAN}.outbox.jsonl').read_text(encoding='utf-8')
        assert raw == json.dumps(record, default=str) + '\n'

    def test_an_unserialisable_value_is_dumped_through_default_str(self):
        record = {'event': 'x', 'when': object()}
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, record)
        raw = (self.legacy_dir / f'{PLAN}.outbox.jsonl').read_text(encoding='utf-8')
        assert raw == json.dumps(record, default=str) + '\n'

    def test_appending_uses_the_shared_rotation_policy(self, monkeypatch):
        calls = []

        def spy(path, max_bytes, keep):
            calls.append((path, max_bytes, keep))

        monkeypatch.setattr(persistence, '_rotate_if_needed', spy)
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'event': 'x'})
        assert len(calls) == 1
        rotated_path, max_bytes, keep = calls[0]
        assert Path(rotated_path).name == f'{PLAN}.outbox.jsonl'
        assert max_bytes == persistence.NOTIFICATIONS_MAX_BYTES
        assert keep == persistence.NOTIFICATIONS_KEEP_N

    # -- reading -------------------------------------------------------
    def test_reading_a_queue_that_was_never_written_creates_no_file(self):
        assert self._store().read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY) == []
        assert list(self.legacy_dir.iterdir()) == []
        assert list(self.instance_dir.iterdir()) == []

    def test_a_malformed_line_is_skipped_and_logged_without_its_content(
        self, caplog
    ):
        path = self.legacy_dir / f'{PLAN}.outbox.{INSTANCE}.jsonl'
        path.write_text('this is not json\n', encoding='utf-8')
        with caplog.at_level(logging.WARNING):
            entries = self._store().read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert entries == []
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and PLAN in record.getMessage()
        ]
        assert len(warnings) == 1
        assert 'this is not json' not in caplog.text

    def test_a_json_line_that_is_not_an_object_is_skipped(self):
        path = self.instance_dir / f'{PLAN}.outbox.{INSTANCE}.jsonl'
        path.write_text('[1, 2, 3]\n', encoding='utf-8')
        assert self._store().read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []

    # -- acking --------------------------------------------------------
    def test_a_malformed_line_is_dropped_by_the_next_ack_and_logged_once(
        self, caplog
    ):
        path = self.instance_dir / f'{PLAN}.outbox.{INSTANCE}.jsonl'
        path.write_text('not json at all\n', encoding='utf-8')
        with caplog.at_level(logging.WARNING):
            removed = self._store().ack_outbox(
                DEFAULT_NAMESPACE, PLAN, INSTANCE, ['no-such-id']
            )
        assert removed == 0
        assert path.read_text(encoding='utf-8') == ''
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and PLAN in record.getMessage()
        ]
        assert len(warnings) == 1
        assert 'not json at all' not in caplog.text

    def test_an_ack_with_no_ids_leaves_the_file_untouched(self):
        path = self.legacy_dir / f'{PLAN}.outbox.jsonl'
        self._store().append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'event': 'x'})
        before = path.read_bytes()
        assert self._store().ack_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, []) == 0
        assert path.read_bytes() == before

    def test_the_remaining_lines_are_kept_byte_for_byte(self):
        path = self.legacy_dir / f'{PLAN}.outbox.jsonl'
        store = self._store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 1})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 2})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        store.ack_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, [entries[1]['id']])
        remaining = path.read_text(encoding='utf-8').splitlines()
        assert remaining == [
            json.dumps({'n': 0}, default=str),
            json.dumps({'n': 2}, default=str),
        ]

    # -- the ids -------------------------------------------------------
    def test_two_identical_lines_carry_the_same_id(self):
        store = self._store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        assert len(entries) == 2
        assert entries[0]['id'] == entries[1]['id']

    def test_acking_a_shared_id_once_removes_exactly_one_entry(self):
        store = self._store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        removed = store.ack_outbox(
            DEFAULT_NAMESPACE, PLAN, LEGACY, [entries[0]['id']]
        )
        assert removed == 1
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)) == 1

    def test_different_lines_carry_different_ids(self):
        store = self._store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 1})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        assert entries[0]['id'] != entries[1]['id']
