"""Review jobs and merge outcomes are persisted through the record store.

``pipeline.advance_review._persist`` and ``pipeline.advance_merge``'s
adjudication used to do their own ``json.loads(manifest_path.read_text())`` /
``_atomic_write_json(manifest_path, manifest)`` round trip. Both now read
``records().get_manifest("default", plan_name)`` and write
``records().put_manifest("default", plan_name, manifest)`` at the same point in
the same lock scope, so a fake or spying store alone is enough to see the write.

The two failure modes that matter are covered too: a manifest that vanished
between the tick's read and the write raises instead of resurrecting a deleted
plan, and a plan name the store rejects fails closed before any write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.advance as advance_module
import pipeline.review_job as review_job_module
from pipeline import advance_merge, advance_review
from pipeline import server as p
from pipeline.store_records import InvalidRecordKeyError


class _SpyStore:
    """The real file store with every manifest write recorded.

    ``get_manifest`` / ``put_manifest`` delegate to the store the process would
    use anyway, so a test asserts on what actually landed on disk; the write log
    shows whether the site went through the record store at all.
    """

    def __init__(self, inner):
        self._inner = inner
        self.writes: list[tuple[str, str]] = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        return self._inner.records().get_manifest(namespace, plan_name)

    def put_manifest(self, namespace, plan_name, manifest):
        self.writes.append((namespace, plan_name))
        self._inner.records().put_manifest(namespace, plan_name, manifest)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A tmp PLAN_DIR plus a spying store wired into advance's seam."""
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    spy = _SpyStore(p._store)
    monkeypatch.setattr(advance_module, "_store", spy)
    return spy


def _seed(tmp_path: Path, plan_name: str, manifest: dict) -> Path:
    path = tmp_path / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def test_should_persist_a_started_review_job_through_the_record_store(
    tmp_path, monkeypatch, store
):
    """``_start`` records the job handle with ``put_manifest``."""
    plan_name, key = "p1", "s1"
    _seed(
        tmp_path,
        plan_name,
        {"plan": plan_name, "stories": {key: {"status": "tests_passed"}}},
    )
    monkeypatch.setattr(
        review_job_module, "start_review_job", lambda *a, **k: {"pid": 4242}
    )
    summary: dict = {}

    advance_review.review_or_collect(
        plan_name, key, {"status": "tests_passed"}, summary
    )

    assert store.writes == [("default", plan_name)]
    stored = p._store.records().get_manifest("default", plan_name)
    assert stored["stories"][key]["review_job"] == {"pid": 4242}
    assert summary["review_started"] == [key]


def test_should_drop_a_finished_job_through_the_record_store(
    tmp_path, monkeypatch, store
):
    """``_apply`` drops the handle with ``put_manifest``, once."""
    plan_name, key = "p2", "s1"
    _seed(
        tmp_path,
        plan_name,
        {
            "plan": plan_name,
            "stories": {
                key: {"status": "tests_passed", "review_job": {"pid": 4242}}
            },
        },
    )
    monkeypatch.setattr(
        review_job_module,
        "collect_review_job",
        lambda *a, **k: {"state": "done", "status": "in_review"},
    )
    summary: dict = {}
    story = {"status": "tests_passed", "review_job": {"pid": 4242}}

    advance_review.review_or_collect(plan_name, key, story, summary)

    assert store.writes == [("default", plan_name)]
    stored = p._store.records().get_manifest("default", plan_name)
    assert "review_job" not in stored["stories"][key]
    assert summary["advanced"] == [{key: "in_review"}]


def test_should_persist_the_merge_outcome_through_the_record_store(store, tmp_path):
    """``_adjudicate_merges`` writes the manifest back through the store."""
    plan_name = "p3"
    manifest = {"plan": plan_name, "merged": True, "stories": {}}
    _seed(tmp_path, plan_name, manifest)

    advance_merge._adjudicate_merges(plan_name, {"merged": [], "parked": []})

    assert store.writes == [("default", plan_name)]
    stored = p._store.records().get_manifest("default", plan_name)
    assert stored == manifest
    assert stored["merged"] is True


def test_should_raise_and_not_write_when_the_manifest_disappeared_between_reads(
    tmp_path, store
):
    """A manifest deleted mid-tick raises; the plan is not resurrected."""
    plan_name = "p4"
    path = tmp_path / f"{plan_name}.manifest.json"
    assert not path.exists()

    def _mutate(stored: dict) -> None:
        stored["status"] = "done"

    with pytest.raises(FileNotFoundError):
        advance_review._persist(plan_name, "s1", _mutate)

    assert store.writes == []
    assert p._store.records().get_manifest("default", plan_name) is None
    assert not path.exists()


def test_should_fail_closed_on_a_plan_name_the_store_rejects(tmp_path, store):
    """``-plan`` passes key validation but not plan-name validation: no write."""
    valid = "p5"
    _seed(tmp_path, valid, {"plan": valid, "stories": {"s1": {"status": "todo"}}})
    path = tmp_path / "-plan.manifest.json"

    with pytest.raises(InvalidRecordKeyError) as excinfo:
        advance_review._persist("-plan", "s1", lambda stored: None)

    assert isinstance(excinfo.value, ValueError)
    assert store.writes == []
    assert not path.exists()
    untouched = p._store.records().get_manifest("default", valid)
    assert untouched == {"plan": valid, "stories": {"s1": {"status": "todo"}}}
