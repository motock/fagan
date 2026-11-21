"""run_triage_sweep persists the manifest in the namespace bound for the call."""

from __future__ import annotations

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import triage as triage_mod
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN = "nsweep"
NAMESPACE = "acme/team"


@pytest.fixture
def records(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
    monkeypatch.setattr(triage_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(triage_mod, "classify_repo_health", lambda *a, **k: [])
    monkeypatch.setattr(triage_mod, "collect_triage_evidence", lambda *a, **k: "")
    monkeypatch.setattr(
        triage_mod,
        "rule_on_story",
        lambda *a, **k: {"action": "park_for_human", "rationale": "synthetic"},
    )
    store = InMemoryRecordStore()
    monkeypatch.setattr(p._store, "records", lambda: store)
    return store


def test_sweep_writes_the_manifest_to_the_bound_namespace_not_default(records):
    story = {"status": "parked", "parked_reason": "stuck", "worktree": "", "triage_attempts": 0, "triage_actions": []}
    records.put_manifest(NAMESPACE, PLAN, {"stories": {"A": story}})

    with bind_namespace(NAMESPACE):
        result = triage_mod.run_triage_sweep(PLAN)

    assert result["triaged"] == ["A"]
    assert records.get_manifest(NAMESPACE, PLAN)["stories"]["A"]["triage_attempts"] == 1
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN) is None
