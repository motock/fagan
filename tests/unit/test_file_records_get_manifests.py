"""Tests for the bulk manifest read on ``FileRecords`` (ADR 0012, stage B)."""

from __future__ import annotations

import json

import pytest

from pipeline import cross_plan, persistence, server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    cross_plan.clear_manifest_cache()
    yield d
    cross_plan.clear_manifest_cache()


def _write_raw(plan_dir, plan_name, text):
    (plan_dir / f'{plan_name}.manifest.json').write_text(text)


def _write(plan_dir, plan_name, manifest):
    _write_raw(plan_dir, plan_name, json.dumps(manifest))


def test_should_skip_a_malformed_manifest_and_return_the_rest(plan_dir):
    _write(plan_dir, 'alpha', {'x': 1})
    _write_raw(plan_dir, 'broken', '{"x": ')
    _write(plan_dir, 'gamma', {'x': 3})

    result = FileStore().records().get_manifests(DEFAULT_NAMESPACE)

    assert result == {'alpha': {'x': 1}, 'gamma': {'x': 3}}
    assert list(result) == ['alpha', 'gamma']


def test_should_skip_a_manifest_that_is_not_a_json_object(plan_dir):
    _write(plan_dir, 'arr', [1, 2, 3])
    _write(plan_dir, 'txt', 'hello')
    _write(plan_dir, 'good', {'ok': True})

    result = FileStore().records().get_manifests(DEFAULT_NAMESPACE)

    assert result == {'good': {'ok': True}}


def test_should_return_the_same_parsed_object_for_an_unchanged_manifest(plan_dir):
    _write(plan_dir, 'alpha', {'x': 1})
    records = FileStore().records()

    first = records.get_manifests(DEFAULT_NAMESPACE)
    second = records.get_manifests(DEFAULT_NAMESPACE)

    assert first['alpha'] == {'x': 1}
    assert first['alpha'] is second['alpha']


def test_should_reread_a_manifest_after_it_changes(plan_dir):
    _write(plan_dir, 'alpha', {'v': 1})
    records = FileStore().records()

    first = records.get_manifests(DEFAULT_NAMESPACE)
    _write(plan_dir, 'alpha', {'v': 2, 'extra': True})
    second = records.get_manifests(DEFAULT_NAMESPACE)
    third = records.get_manifests(DEFAULT_NAMESPACE)

    assert first['alpha'] == {'v': 1}
    assert second['alpha'] == {'v': 2, 'extra': True}
    assert second['alpha'] is not first['alpha']
    assert third['alpha'] is second['alpha']
