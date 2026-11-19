"""coa-01: every failure-triage and high-risk-merge ruling records what the
overlord was shown, so a past ruling can be replayed.

The snapshot is the ``input`` object on each ruling record. It carries the
redacted, capped question part of the prompt and digests of the prompt and
policy text - never the raw prompt. The overlord itself is stubbed at the
``_invoke_overlord`` boundary, as the existing triage and merge suites do.
"""

from __future__ import annotations

import hashlib
import types

import pytest

# pipeline.server is imported first: pipeline modules have a pre-existing
# import cycle (see tests/unit/test_triage_cap_silent_skip.py).
import pipeline.server  # noqa: F401
from app import dashboard_routes, role_registry
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import ruling_snapshot as snap
from pipeline import server as server_mod
from pipeline import triage as triage_mod
from pipeline import triage_outcomes as outcomes_mod
from pipeline import triage_readiness as readiness_mod

GHP_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
POLICY_STUB = "POLICY-STUB-TEXT: decide carefully.\n"
EVIDENCE_MARKER = "EVIDENCE-MARKER: the last test run failed on import"

KEYS = {
    "evidence",
    "truncated",
    "prompt_sha256",
    "policy_sha256",
    "context_digests",
    "backend",
    "model",
    "park_class",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def resolved_overlord(monkeypatch):
    """Pin the overlord's backend/model resolution so the snapshot is deterministic."""
    monkeypatch.setattr(
        role_registry,
        "resolve_role",
        lambda role, **kwargs: types.SimpleNamespace(provider="stub-backend", model="stub-model"),
    )


def _snapshot(**overrides):
    kwargs = {
        "evidence": "some evidence",
        "prompt": "full prompt",
        "policy": "policy text",
        "context_digests": None,
        "park_class": "stuck_in_loop",
        "plan_role_config": None,
    }
    kwargs.update(overrides)
    return snap.ruling_input(**kwargs)


# --------------------------------------------------------------------------
# ruling_input: the snapshot shape and its guarantees
# --------------------------------------------------------------------------


def test_ruling_input_has_exactly_the_documented_keys(resolved_overlord):
    assert set(_snapshot()) == KEYS


def test_ruling_input_redacts_a_github_token_in_evidence(resolved_overlord):
    result = _snapshot(evidence=f"push failed with {GHP_TOKEN}")

    assert GHP_TOKEN not in result["evidence"]
    assert "[redacted]" in result["evidence"]


def test_ruling_input_redacts_a_named_password_in_evidence(resolved_overlord):
    result = _snapshot(evidence="login used password=hunter2 and failed")

    assert "hunter2" not in result["evidence"]
    assert "password=" in result["evidence"]


def test_ruling_input_cuts_evidence_over_the_cap_and_flags_it(resolved_overlord):
    result = _snapshot(evidence="x" * (snap.EVIDENCE_MAX_CHARS + 1))

    assert len(result["evidence"]) == 16000
    assert result["truncated"] is True


def test_ruling_input_keeps_evidence_exactly_at_the_cap_untruncated(resolved_overlord):
    result = _snapshot(evidence="y" * snap.EVIDENCE_MAX_CHARS)

    assert len(result["evidence"]) == 16000
    assert result["truncated"] is False


def test_ruling_input_empty_evidence_is_empty_and_untruncated(resolved_overlord):
    result = _snapshot(evidence="")

    assert result["evidence"] == ""
    assert result["truncated"] is False


def test_ruling_input_prompt_digest_is_none_without_a_prompt(resolved_overlord):
    assert _snapshot(prompt=None)["prompt_sha256"] is None


def test_ruling_input_digests_the_full_prompt_in_utf8(resolved_overlord):
    prompt = "policy\nTRIAGE QUESTION: café ✓"

    assert _snapshot(prompt=prompt)["prompt_sha256"] == _sha256(prompt)


def test_ruling_input_digests_the_policy_text(resolved_overlord):
    assert _snapshot(policy=POLICY_STUB)["policy_sha256"] == _sha256(POLICY_STUB)


def test_ruling_input_policy_digest_is_none_without_policy(resolved_overlord):
    assert _snapshot(policy=None)["policy_sha256"] is None


def test_ruling_input_context_digests_default_to_an_empty_list(resolved_overlord):
    assert _snapshot(context_digests=None)["context_digests"] == []


def test_ruling_input_passes_given_context_digests_through(resolved_overlord):
    assert _snapshot(context_digests=["abc", "def"])["context_digests"] == ["abc", "def"]


def test_ruling_input_records_the_resolved_overlord_backend_and_model(resolved_overlord):
    result = _snapshot()

    assert result["backend"] == "stub-backend"
    assert result["model"] == "stub-model"


def test_ruling_input_sets_backend_and_model_to_none_when_resolution_raises(monkeypatch):
    def _boom(role, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(role_registry, "resolve_role", _boom)

    result = _snapshot()

    assert result["backend"] is None
    assert result["model"] is None


def test_ruling_input_records_the_park_class_it_was_given(resolved_overlord):
    assert _snapshot(park_class="high_risk_merge")["park_class"] == "high_risk_merge"


def test_ruling_input_never_stores_the_raw_prompt_or_policy(resolved_overlord):
    prompt = f"{POLICY_STUB}TRIAGE QUESTION: token {GHP_TOKEN}"

    result = _snapshot(prompt=prompt, policy=POLICY_STUB)

    stored = [v for v in result.values() if isinstance(v, str)]
    assert not any(prompt in v or POLICY_STUB in v for v in stored)
    assert GHP_TOKEN not in repr(result)


# --------------------------------------------------------------------------
# failure triage: rule_on_story writes the snapshot on its record
# --------------------------------------------------------------------------


class _TriageHarness:
    def __init__(self, monkeypatch, reply="RULING: park\nRATIONALE: stub"):
        self.prompts = []
        self.records = []
        self.reply = reply
        monkeypatch.setattr(triage_mod, "_load_policy", lambda: POLICY_STUB)
        monkeypatch.setattr(triage_mod, "_plan_role_config", lambda plan_name: {})
        monkeypatch.setattr(
            triage_mod,
            "_append_decision",
            lambda plan_name, record: self.records.append(record),
        )
        monkeypatch.setattr(
            triage_mod,
            "_invoke_overlord",
            lambda prompt, plan_role_config=None: self._reply(prompt),
        )

    def _reply(self, prompt):
        self.prompts.append(prompt)
        return self.reply

    def rule(self, evidence=EVIDENCE_MARKER):
        story = {"status": "failed", "last_test_check": {"stdout_tail": "boom"}}
        return triage_mod.rule_on_story("PLAN-1", "S-1", story, evidence)


def _failure_triage_record(records):
    return next(r for r in records if r.get("question") == "failure triage")


def test_failure_triage_record_carries_an_input_object(monkeypatch, resolved_overlord):
    harness = _TriageHarness(monkeypatch)

    harness.rule()

    assert "input" in _failure_triage_record(harness.records)


def test_failure_triage_input_park_class_matches_the_record(monkeypatch, resolved_overlord):
    harness = _TriageHarness(monkeypatch)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert record["input"]["park_class"] == record["park_class"]


def test_failure_triage_input_prompt_digest_is_the_prompt_sent_to_the_overlord(
    monkeypatch, resolved_overlord
):
    harness = _TriageHarness(monkeypatch)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert record["input"]["prompt_sha256"] == _sha256(harness.prompts[0])


def test_failure_triage_input_policy_digest_is_the_policy_text(monkeypatch, resolved_overlord):
    harness = _TriageHarness(monkeypatch)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert record["input"]["policy_sha256"] == _sha256(POLICY_STUB)


def test_failure_triage_input_evidence_omits_the_policy_text(monkeypatch, resolved_overlord):
    harness = _TriageHarness(monkeypatch)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert POLICY_STUB not in record["input"]["evidence"]
    assert EVIDENCE_MARKER in record["input"]["evidence"]


def test_failure_triage_policy_plus_input_evidence_rebuilds_the_prompt(
    monkeypatch, resolved_overlord
):
    harness = _TriageHarness(monkeypatch)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert POLICY_STUB + record["input"]["evidence"] == harness.prompts[0]


def test_failure_triage_input_is_recorded_when_the_overlord_call_fails(
    monkeypatch, resolved_overlord
):
    harness = _TriageHarness(monkeypatch)
    failed_prompts = []

    def _fail(prompt, plan_role_config=None):
        failed_prompts.append(prompt)
        raise RuntimeError("backend down")

    monkeypatch.setattr(triage_mod, "_invoke_overlord", _fail)

    harness.rule()
    record = _failure_triage_record(harness.records)

    assert record["input"]["prompt_sha256"] == _sha256(failed_prompts[0])
    assert record["input"]["park_class"] == record["park_class"]


# --------------------------------------------------------------------------
# high-risk merge adjudication: both record kinds carry the snapshot
# --------------------------------------------------------------------------

PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green"
GARBAGE_REPLY = "I am not following the format at all"


class _MergeHarness:
    def __init__(self, monkeypatch, reply):
        self.decisions = []
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
        monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
        monkeypatch.setattr(
            overlord_mod,
            "_invoke_overlord",
            lambda prompt, plan_role_config=None: reply,
        )
        monkeypatch.setattr(
            persistence_mod, "_plan_role_config", lambda plan_name: {"role": "overlord"}
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append(record),
        )

    def decide(self):
        story = {
            "key": "OPSA-COA-01",
            "status": "pr_open",
            "parked_reason": None,
            "review_verdict": "APPROVE",
            "security_review_verdict": "APPROVE",
            "risk": "high",
            "summary": "Rewrite the auth token cache",
            "pr_checks": {"ci": "pass", "lint": "pass"},
        }
        with merge_mod.merge_adjudication_plan("PLAN-1"):
            return merge_mod._merge_decision(story)


def _merge_records(decisions):
    return [r for r in decisions if r.get("question") == "high-risk merge adjudication"]


def test_parsed_merge_adjudication_carries_an_input_object(monkeypatch, resolved_overlord):
    harness = _MergeHarness(monkeypatch, PROCEED_REPLY)

    harness.decide()
    (record,) = _merge_records(harness.decisions)

    assert record["input"]["park_class"] == "high_risk_merge"
    assert record["input"]["policy_sha256"] is None
    assert record["input"]["prompt_sha256"] is not None


def test_unparseable_merge_adjudication_carries_an_input_object(monkeypatch, resolved_overlord):
    harness = _MergeHarness(monkeypatch, GARBAGE_REPLY)

    harness.decide()
    (record,) = _merge_records(harness.decisions)

    assert record["ruling"] == ""
    assert record["input"]["park_class"] == "high_risk_merge"
    assert record["input"]["prompt_sha256"] is not None


def test_merge_adjudication_input_evidence_is_the_whole_prompt(monkeypatch, resolved_overlord):
    harness = _MergeHarness(monkeypatch, PROCEED_REPLY)
    prompts = []
    monkeypatch.setattr(
        overlord_mod,
        "_invoke_overlord",
        lambda prompt, plan_role_config=None: prompts.append(prompt) or PROCEED_REPLY,
    )

    harness.decide()
    (record,) = _merge_records(harness.decisions)

    assert record["input"]["evidence"] == prompts[0]


# --------------------------------------------------------------------------
# old records (no input) are still read by the readers, unchanged
# --------------------------------------------------------------------------


def _old_failure_ruling(decided_at="2026-11-01T00:00:00+00:00"):
    return {
        "question": "failure triage",
        "story_key": "S-1",
        "action": "park_for_human",
        "park_class": "stuck_in_loop",
        "ruling": "park",
        "decided_by": "overlord-triage",
        "decided_at": decided_at,
    }


def test_readiness_reads_an_old_record_without_input(monkeypatch):
    old = _old_failure_ruling()
    assert "input" not in old
    resolution = {
        "question": "human resolution",
        "story_key": "S-1",
        "ruling_ref": old["decided_at"],
        "human_action": "park",
    }

    verdict = readiness_mod.readiness([old, resolution])["stuck_in_loop"]

    assert verdict["shadow_count"] == 1
    assert verdict["paired_count"] == 1
    assert verdict["agree"] == 1


def test_outcomes_read_an_old_record_without_input(monkeypatch):
    old = _old_failure_ruling()
    monkeypatch.setattr(outcomes_mod, "_read_decisions", lambda plan_name: [old])

    assert outcomes_mod.latest_failure_ruling("PLAN-1", "S-1") == old


# --------------------------------------------------------------------------
# dashboard plan detail omits input and keeps every other key
# --------------------------------------------------------------------------


def test_plan_detail_omits_input_from_each_decision(monkeypatch):
    decision = {
        "question": "failure triage",
        "story_key": "S-1",
        "ruling": "park",
        "park_class": "stuck_in_loop",
        "decided_at": "2026-11-01T00:00:00+00:00",
        "input": {"evidence": "x" * 100, "prompt_sha256": "abc"},
    }

    class _FakeStore:
        def get_decisions(self, plan_name):
            return [decision, "not-a-dict"]

        def get_notifications(self, plan_name):
            return []

        def get_notification_records(self, plan_name):
            return []

    monkeypatch.setattr(
        dashboard_routes._service,
        "get_manifest_or_none",
        lambda plan_name: {"stories": {}, "epics": {}},
    )
    monkeypatch.setattr(dashboard_routes, "_store", _FakeStore())

    decisions = dashboard_routes.get_plan("PLAN-1")["decisions"]

    expected = {k: v for k, v in decision.items() if k != "input"}
    assert decisions == [expected, "not-a-dict"]
    assert "input" not in decisions[0]
