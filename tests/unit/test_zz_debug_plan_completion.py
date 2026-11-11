"""Scratch debug test — deleted before commit."""

import pytest

from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-completion'
OTHER_PLAN = 'other-plan'


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
    for backend, store in stores.items():
        print('START', backend, sorted(p.name for p in plan_dir.iterdir()))
        store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN)
        print('AFTER MARK', backend, sorted(p.name for p in plan_dir.iterdir()))
        print('ISDONE OTHER', backend,
              store.is_plan_completed(DEFAULT_NAMESPACE, OTHER_PLAN))
        print('AFTER ISDONE', backend, sorted(p.name for p in plan_dir.iterdir()))
