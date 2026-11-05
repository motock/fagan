"""Tests for the ``FileStore`` namespaced record view (ADR 0012, stage A)."""

from __future__ import annotations

import pytest

from pipeline import persistence, server
from pipeline.store import FileRecords, FileStore
from pipeline.store_contract import RecordStoreContract, SingleNamespaceContract
from pipeline.store_records import DEFAULT_NAMESPACE


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


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
