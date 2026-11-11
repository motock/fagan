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


@pytest.fixture
def stores(plan_dir, tmp_path):
    sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
    try:
        yield {
            'file': FileStore().records(),
            'sqlite': sqlite_store.records(),
        }
    finally:
        sqlite_store.close()


def test_debug(stores, plan_dir):
    sqlite_records = stores['sqlite']
    delegate = sqlite_records._draft_records()
    print('DELEGATE TYPE', type(delegate))
    print('STOREDIR', delegate._store.manifest_path('plan-completion'))
    print('SQLITE DIR', delegate._plan_path('plan-completion', '.plan_completed'))
    print('SQLITE OTHER', delegate._plan_path('other-plan', '.plan_completed'))
    print('MARK', sqlite_records.mark_plan_completed(DEFAULT_NAMESPACE, 'plan-completion'))
    print('ISDONE OTHER', sqlite_records.is_plan_completed(
        DEFAULT_NAMESPACE, 'other-plan'))
    print('FILES', [p.name for p in plan_dir.iterdir()])
    print('EXISTS PLANC', delegate._plan_path(
        'plan-completion', '.plan_completed').exists())
