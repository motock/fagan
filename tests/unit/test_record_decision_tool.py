"""Tests for the ``record_decision`` MCP tool (S3).

The tool records one judgment call in the plan's hash-chained decision stream.
These tests pin the success shape, every refusal code, the per-principal rate
limit (including the boundary arithmetic and that a refusal consumes no slot),
the identity rule (the bound principal, never an argument), the tier the v2
chain allows a non-human principal, the plan manifest's story check, the remote
registration, and that no ruling or rationale text ever reaches the audit chain.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.identity import SYSTEM_WORKER, Principal
from app.policy import Action
from pipeline import audit, decision_records, paths, persistence
from pipeline import decision_record_tool as tool
from pipeline.actor import bind_principal
from pipeline.remote_tool_policy import (
    REMOTE_ENFORCED,
    REMOTE_READ,
    remote_tool_allowed,
)
from pipeline.server_tools_lifecycle import TOOLS

PLAN = "plan-alpha"
STORY = "s1"
AGENT = Principal(
    subject="agent:planner-7",
    auth_method="oidc",
    principal_type="agent",
    team_scope="acme/team-alpha",
)
REMOTE_HUMAN = Principal(
    subject="human:mallory", auth_method="oidc", principal_type="human"
)

EVIDENCE = [{"kind": "test_run", "ref": "pytest -q tests/unit/test_x.py", "sha256": ""}]

MANIFEST: dict[str, Any] = {
    "team": "team-alpha",
    "owner": AGENT.subject,
    "delegates": [],
    "stories": {STORY: {"key": STORY, "title": "a story"}},
}


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
    """Redirect both plan-directory bindings, so no test touches real plans."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv("PIPELINE_AUTHORIZER_PLUGIN", raising=False)
    monkeypatch.delenv("PIPELINE_AUDIT_ANCHOR", raising=False)
    monkeypatch.setenv("PIPELINE_RECORD_DECISION_PER_MINUTE", "30")
    tool._WINDOWS.clear()
    yield directory
    tool._WINDOWS.clear()


@pytest.fixture()
def manifest(plan_dir, monkeypatch):
    """Serve a fixed manifest, so the story check needs no real plan on disk."""
    monkeypatch.setattr(
        tool, "_manifest", lambda plan_name: dict(MANIFEST) if plan_name == PLAN else {}
    )
    return MANIFEST


def _call(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "plan_name": PLAN,
        "story_key": STORY,
        "decision_class": "high_risk_merge",
        "question": "merge the high-risk change?",
        "ruling": "merge",
        "rationale": "ci is green and the diff is one line",
        "evidence": [dict(item) for item in EVIDENCE],
    }
    arguments.update(overrides)
    return tool.record_decision(**arguments)


def _audits(plan_name: str = PLAN) -> list[dict[str, Any]]:
    path = audit.audit_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _records(plan_name: str = PLAN) -> list[dict[str, Any]]:
    return decision_records.read_decision_records(plan_name)


# --------------------------------------------------------------------------
# Success
# --------------------------------------------------------------------------


def test_success_appends_one_chained_record_and_one_audit(manifest) -> None:
    result = _call()

    assert result["recorded"] is True
    assert isinstance(result["seq"], int)
    assert result["decision_id"] and len(result["decision_id"]) == 32

    records = _records()
    assert len(records) == 1
    record = records[0]
    assert record["decision_id"] == result["decision_id"]
    assert record["seq"] == result["seq"] == 0
    assert record["class"] == "high_risk_merge"
    assert record["subject"] == {"plan": PLAN, "story": STORY, "pr": "", "sha": ""}
    assert record["tier"] == "human"  # nothing bound: the local operator records
    assert record["outcome"] == ""
    assert record["rule"]["layer_shas"] == []
    assert record["rule"]["persona_sha"] == ""
    assert record["rule"]["policy_sha"] == ""
    assert record["rule"]["model"]
    assert record["alternatives"] == [
        "rationale: ci is green and the diff is one line"
    ]
    assert record["evidence"] == [{"kind": "test_run", "ref": EVIDENCE[0]["ref"], "sha256": ""}]
    assert decision_records.verify_decision_chain(PLAN) is None

    recorded = [entry for entry in _audits() if entry["outcome"] == "recorded"]
    assert len(recorded) == 1
    assert recorded[0]["action"] == "decision:record"
    assert recorded[0]["target"] == {"story": STORY}
    assert recorded[0]["detail"] == {"decision_id": result["decision_id"]}


def test_rationale_is_truncated_to_fit_the_alternatives_limit(manifest) -> None:
    result = _call(rationale="R" * 250)

    assert result["recorded"] is True
    entry = _records()[0]["alternatives"]
    assert entry == ["rationale: " + "R" * (tool.ALTERNATIVES_LIMIT - 11)]
    assert len(entry[0]) == tool.ALTERNATIVES_LIMIT


def test_extra_actor_argument_does_not_change_the_identity(manifest) -> None:
    with bind_principal(AGENT):
        result = _call(actor="human:bob")

    assert result["recorded"] is True
    record = _records()[0]
    assert record["actor"] == AGENT.subject
    # v2 binds tier to the transport identity: an agent may not claim "human".
    assert record["tier"] == "auto"


def test_agent_principal_may_record_a_judgment(manifest) -> None:
    with bind_principal(AGENT):
        result = _call()

    assert result["recorded"] is True
    assert _records()[0]["actor"] == AGENT.subject


def test_system_worker_is_not_authorized(manifest) -> None:
    with bind_principal(SYSTEM_WORKER):
        result = _call()

    assert result == {"recorded": False, "error": "not_authorized"}
    assert _records() == []


def test_manifest_is_loaded_through_the_authorization_loader(plan_dir, monkeypatch) -> None:
    seen: list[str] = []

    def fake(plan_name: str) -> dict[str, Any]:
        seen.append(plan_name)
        return {}

    monkeypatch.setattr("pipeline.authorization._load_manifest", fake)
    tool._manifest(PLAN)

    assert seen == [PLAN]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ruling", ["", "   ", "\n\t "])
def test_empty_ruling_is_refused(manifest, ruling) -> None:
    assert _call(ruling=ruling) == {"recorded": False, "error": "empty_ruling"}
    assert _records() == []


@pytest.mark.parametrize("rationale", ["", "   "])
def test_empty_rationale_is_refused(manifest, rationale) -> None:
    assert _call(rationale=rationale) == {"recorded": False, "error": "empty_rationale"}
    assert _records() == []


def test_ruling_is_checked_before_rationale(manifest) -> None:
    result = _call(ruling="", rationale="")

    assert result["error"] == "empty_ruling"


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        "not-a-list",
        [{"ref": "x"}],
        [{"kind": "test_run"}],
        [{"kind": "  ", "ref": "x"}],
        [{"kind": "test_run", "ref": "   "}],
        ["not-a-mapping"],
    ],
)
def test_empty_evidence_is_refused(manifest, evidence) -> None:
    assert _call(evidence=evidence) == {"recorded": False, "error": "empty_evidence"}
    assert _records() == []


def test_one_valid_evidence_item_is_enough(manifest) -> None:
    result = _call(evidence=[{"kind": "  "}, {"kind": "note", "ref": "retro/notes.md"}])

    assert result["recorded"] is True
    assert _records()[0]["evidence"] == [
        {"kind": "note", "ref": "retro/notes.md", "sha256": ""}
    ]


@pytest.mark.parametrize("decision_class", ["", "nope", "HIGH_RISK_MERGE", None, 42])
def test_invalid_decision_class_is_refused(manifest, decision_class) -> None:
    result = _call(decision_class=decision_class)

    assert result == {"recorded": False, "error": "invalid_decision_class"}
    assert _records() == []


def test_decision_class_from_the_allowed_list_is_accepted(manifest) -> None:
    assert _call(decision_class="triage")["recorded"] is True


def test_unknown_story_is_refused(manifest) -> None:
    assert _call(story_key="nope") == {"recorded": False, "error": "unknown_story"}
    assert _records() == []


def test_refusal_dicts_carry_a_code_and_echo_no_input(manifest) -> None:
    sentinel = "SENTINEL-RULING-VALUE"
    result = _call(story_key="nope", ruling=sentinel)

    assert set(result) == {"recorded", "error"}
    assert sentinel not in json.dumps(result)


# --------------------------------------------------------------------------
# Hidden versus missing plan
# --------------------------------------------------------------------------


def test_hidden_plan_and_missing_plan_are_refused_identically(manifest) -> None:
    with bind_principal(REMOTE_HUMAN):
        hidden = _call()
        # The same principal, a plan the manifest does not describe at all.
        missing = _call(plan_name="plan-that-does-not-exist")

    assert hidden == missing == {"recorded": False, "error": "not_authorized"}
    assert _records() == []


def test_denied_call_is_not_double_audited(manifest) -> None:
    with bind_principal(REMOTE_HUMAN):
        _call()

    assert [entry["outcome"] for entry in _audits()] == ["denied"]


# --------------------------------------------------------------------------
# Rate limit
# --------------------------------------------------------------------------


def _freeze(monkeypatch, clock: dict[str, float]) -> None:
    monkeypatch.setattr(tool, "_now", lambda: clock["t"])


def test_thirty_calls_are_recorded_and_the_thirty_first_is_refused(
    manifest, monkeypatch
) -> None:
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    for tick in range(30):
        clock["t"] = float(tick)
        assert _call()["recorded"] is True

    clock["t"] = 30.0
    assert _call() == {
        "recorded": False,
        "error": "rate_limited",
        "retry_after_seconds": 30,
    }
    # A refusal must not consume a slot, so the advertised retry still works.
    clock["t"] = 59.9
    assert _call() == {
        "recorded": False,
        "error": "rate_limited",
        "retry_after_seconds": 1,
    }
    clock["t"] = 60.0
    assert _call()["recorded"] is True
    clock["t"] = 60.5
    assert _call() == {
        "recorded": False,
        "error": "rate_limited",
        "retry_after_seconds": 1,
    }


def test_rate_limit_is_per_principal(manifest, monkeypatch) -> None:
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    for tick in range(30):
        clock["t"] = float(tick)
        assert _call()["recorded"] is True

    clock["t"] = 30.0
    assert _call()["error"] == "rate_limited"
    with bind_principal(AGENT):
        assert _call()["recorded"] is True


@pytest.mark.parametrize("value", ["abc", "0", "-5", "  ", "3.5"])
def test_unusable_limit_means_thirty(manifest, monkeypatch, value) -> None:
    monkeypatch.setenv("PIPELINE_RECORD_DECISION_PER_MINUTE", value)
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    for tick in range(30):
        clock["t"] = float(tick)
        assert _call()["recorded"] is True
    assert _call()["error"] == "rate_limited"


def test_a_missing_limit_means_thirty(manifest, monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_RECORD_DECISION_PER_MINUTE", raising=False)
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    for tick in range(30):
        clock["t"] = float(tick)
        assert _call()["recorded"] is True
    assert _call()["error"] == "rate_limited"


def test_a_validated_refusal_does_not_consume_a_slot(manifest, monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_RECORD_DECISION_PER_MINUTE", "2")
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    assert _call()["recorded"] is True
    clock["t"] = 1.0
    assert _call(ruling="")["error"] == "empty_ruling"
    clock["t"] = 2.0
    assert _call()["recorded"] is True
    clock["t"] = 3.0
    assert _call()["error"] == "rate_limited"


def test_rate_limited_refusal_is_audited(manifest, monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_RECORD_DECISION_PER_MINUTE", "1")
    clock = {"t": 0.0}
    _freeze(monkeypatch, clock)

    assert _call()["recorded"] is True
    clock["t"] = 1.0
    assert _call()["error"] == "rate_limited"

    assert [entry["outcome"] for entry in _audits()] == ["recorded", "refused"]
    assert _audits()[1]["detail"] == {"decision_id": None}


# --------------------------------------------------------------------------
# Audit data minimisation
# --------------------------------------------------------------------------


def test_neither_ruling_nor_rationale_reaches_the_audit_chain(manifest) -> None:
    ruling_sentinel = "SENTINEL-RULING-9f31"
    rationale_sentinel = "SENTINEL-RATIONALE-7c02"

    result = _call(ruling=ruling_sentinel, rationale=rationale_sentinel)

    assert result["recorded"] is True
    payload = audit.audit_path(PLAN).read_text()
    assert ruling_sentinel not in payload
    assert rationale_sentinel not in payload


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_tool_is_registered_for_the_remote_transport() -> None:
    assert tool.record_decision in TOOLS
    assert "record_decision" in REMOTE_ENFORCED
    assert "record_decision" not in REMOTE_READ
    assert remote_tool_allowed("record_decision") is True
    assert Action.DECISION_RECORD.value == "decision:record"
