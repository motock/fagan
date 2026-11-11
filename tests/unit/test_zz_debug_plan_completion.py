"""Scratch debug test — deleted before commit."""

import pytest

from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


def test_debug(plan_dir, tmp_path):
    sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
    file_store = FileStore().records()
    sqlite_records = sqlite_store.records()
    print('FILE DIR', file_store._plan_path('plan-completion', '.plan_completed'))
    print('SQLITE DIR', sqlite_records._draft_records()._plan_path(
        'plan-completion', '.plan_completed'))
    print('SQLITE OTHER', sqlite_records._draft_records()._plan_path(
        'other-plan', '.plan_completed'))
    print('MARK', sqlite_records.mark_plan_completed(DEFAULT_NAMESPACE, 'plan-completion'))
    print('ISDONE OTHER', sqlite_records.is_plan_completed(
        DEFAULT_NAMESPACE, 'other-plan'))
    print('FILES', [p.name for p in plan_dir.iterdir()])
