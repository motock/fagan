"""One digest per triage sweep: ``pipeline.triage_digest.sweep_digest``.

The overlord's actions reach the operator as one notification per story. An
operator who turns the sweep on also needs one line per sweep saying what it
did, would have done, and held. These tests pin that digest's shape and the
single ``triage_sweep_summary`` emission from ``run_triage_sweep``.

Digest content is counts and story keys only: never rationale text or file
contents. Tests here stub the store, the overlord and notifications so no
real backend or plan directory is touched.
"""

import copy

import pytest

from pipeline import server, triage
from pipeline.triage_digest import MAX_DIGEST_CHARS, sweep_digest


def _row(key, outcome="shadow", park_class="rebase_conflict", action="resolve_rebase"):
    return {"story_key": key, "park_class": park_class, "action": action, "outcome": outcome}


# ---------------------------------------------------------------------------
# sweep_digest: empty sweep and content
# ---------------------------------------------------------------------------


def test_empty_sweep_has_no_digest():
    assert sweep_digest([]) is None


def test_digest_counts_rulings_by_park_class_and_action():
    text = sweep_digest([
        _row("S1", park_class="rebase_conflict", action="resolve_rebase"),
        _row("S2", park_class="rebase_conflict", action="resolve_rebase"),
        _row("S3", park_class="unknown", action="park_for_human", outcome="park_for_human"),
    ])
    assert "3 ruling" in text
    assert "rebase_conflict 2" in text
    assert "unknown 1" in text
    assert "resolve_rebase 2" in text
    assert "park_for_human 1" in text


def test_shadow_outcome_is_counted_as_shadow_and_keyed():
    text = sweep_digest([_row("S1", outcome="shadow")])
    assert "executed 0" in text
    assert "shadow 1" in text
    assert "held 0" in text
    assert "Shadow: S1" in text


def test_executed_outcome_is_counted_as_executed_and_keyed():
    text = sweep_digest([_row("S1", outcome="resolve_rebase")])
    assert "executed 1" in text
    assert "shadow 0" in text
    assert "Executed: S1" in text


@pytest.mark.parametrize("outcome", ["dry-run", "park_for_human"])
def test_dry_run_and_park_outcomes_are_held(outcome):
    text = sweep_digest([_row("S1", outcome=outcome)])
    assert "held 1" in text
    assert "Held: S1" in text
    assert "executed 0" in text


def test_digest_keys_each_story_in_exactly_one_group():
    text = sweep_digest([
        _row("S1", outcome="resolve_rebase"),
        _row("S2", outcome="shadow"),
        _row("S3", outcome="park_for_human"),
    ])
    assert "Executed: S1" in text
    assert "Shadow: S2" in text
    assert "Held: S3" in text


def test_digest_never_includes_the_ruling_rationale():
    row = _row("S1")
    row["rationale"] = "SECRET-RATIONALE-TEXT"
    row["diagnosis"] = "SECRET-DIAGNOSIS-TEXT"
    text = sweep_digest([row])
    assert "SECRET-RATIONALE-TEXT" not in text
    assert "SECRET-DIAGNOSIS-TEXT" not in text


# ---------------------------------------------------------------------------
# sweep_digest: length bound
# ---------------------------------------------------------------------------


def test_small_digest_is_not_truncated():
    text = sweep_digest([_row("S1")])
    assert len(text) < MAX_DIGEST_CHARS
    assert not text.endswith("...")


def test_large_sweep_digest_is_truncated_to_the_limit():
    rows = [_row(f"story-{i:04d}-" + "x" * 40) for i in range(200)]
    text = sweep_digest(rows)
    assert MAX_DIGEST_CHARS == 1000
    assert len(text) <= 1000
    assert text.endswith("...")


# ---------------------------------------------------------------------------
# run_triage_sweep emits the digest exactly once
# ---------------------------------------------------------------------------


class _FakeRecords:
    def __init__(self, manifest):
        self._manifest = copy.deepcopy(manifest)

    def get_manifest(self, namespace, plan_name):
        return copy.deepcopy(self._manifest)

    def put_manifest(self, namespace, plan_name, record):
        self._manifest = copy.deepcopy(record)


class _FakeStore:
    def __init__(self, manifest):
        self._records = _FakeRecords(manifest)

    def records(self):
        return self._records


def _parked(reason="rebase conflict"):
    return {"status": "parked", "parked_reason": reason, "triage_attempts": 0, "triage_actions": []}


@pytest.fixture
def sweep(monkeypatch):
    """Run the sweep over a fake store; return the notifications it emitted."""
    notices = []

    def _install(stories):
        monkeypatch.setattr(server, "_store", _FakeStore({"stories": stories}))
        monkeypatch.setattr(triage, "_notify_user", lambda plan, msg, **kw: notices.append((msg, kw)))
        monkeypatch.setattr(triage, "_record_execution", lambda *a, **k: None)
        monkeypatch.setattr(triage, "collect_triage_evidence", lambda *a, **k: "")
        monkeypatch.setattr(triage, "classify_repo_health", lambda *a, **k: [])
        monkeypatch.setattr(triage, "rule_on_story", lambda plan, key, story, evidence: {
            "action": "resolve_rebase",
            "rationale": "rebase now",
            "park_class": "rebase_conflict",
        })
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "shadow")
        return notices

    return _install


def _summaries(notices):
    return [(msg, kw) for msg, kw in notices if kw.get("event") == "triage_sweep_summary"]


def test_sweep_emits_one_summary_for_two_ruled_stories(sweep, monkeypatch):
    notices = sweep({"S1": _parked(), "S2": _parked()})
    monkeypatch.setattr(triage, "TRIAGE_MAX_PER_TICK", 5)
    result = triage.run_triage_sweep("demo")
    assert sorted(result["triaged"]) == ["S1", "S2"]
    summaries = _summaries(notices)
    assert len(summaries) == 1
    assert "Shadow: S1, S2" in summaries[0][0]


def test_sweep_with_no_candidates_emits_no_summary(sweep):
    notices = sweep({"S1": {"status": "done"}})
    triage.run_triage_sweep("demo")
    assert _summaries(notices) == []


def test_disabled_sweep_emits_no_summary(sweep, monkeypatch):
    notices = sweep({"S1": _parked()})
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)
    triage.run_triage_sweep("demo")
    assert _summaries(notices) == []


# ---------------------------------------------------------------------------
# rule_on_story records the observed park class on the ruling
# ---------------------------------------------------------------------------


def test_ruling_carries_the_observed_park_class(monkeypatch):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY", raising=False)
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan: {}, raising=False)
    monkeypatch.setattr(triage, "_append_decision", lambda plan, record: None, raising=False)
    monkeypatch.setattr(
        triage, "_invoke_overlord",
        lambda prompt, plan_role_config=None: "RULING: x\nACTION: park_for_human\n",
    )
    story = {"status": "parked", "parked_reason": "x"}
    ruling = triage.rule_on_story("demo", "S1", story, "EVIDENCE")
    expected = triage.classify_park(story, {"test_output": None}).name
    assert ruling["park_class"] == expected


def test_failed_open_ruling_still_carries_the_park_class(monkeypatch):
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY", raising=False)
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan: {}, raising=False)
    monkeypatch.setattr(triage, "_append_decision", lambda plan, record: None, raising=False)

    def _down(prompt, plan_role_config=None):
        raise RuntimeError("overlord unreachable")

    monkeypatch.setattr(triage, "_invoke_overlord", _down)
    ruling = triage.rule_on_story("demo", "S1", {"status": "parked"}, "EVIDENCE")
    assert ruling["failed_open"] is True
    assert "park_class" in ruling
