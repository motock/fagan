"""Tests for the park-class wiring in pipeline.triage (shadow-gated rulings).

Written FIRST (TDD). Covers: the observed park class and allowed actions in
the overlord prompt; replacement of a disallowed ruling with park_for_human;
PIPELINE_TRIAGE_MODE parsing (default and bogus values are shadow); shadow
mode recording new actions without mutating the story, whatever
PIPELINE_AUTONOMY is; and execute mode failing closed on a stub executor.
"""

import copy
import json

import pytest

from pipeline import server, triage

RESOLVE_REBASE_RULING = (
    "RULING: rebase conflict on a fixture\n"
    "TIER: hard\n"
    "RISK: low\n"
    "RATIONALE: the conflict is mechanical\n"
    "NOTIFY_USER: yes\n"
    "ACTION: resolve_rebase\n"
)

AMEND_SCOPE_RULING = RESOLVE_REBASE_RULING.replace("ACTION: resolve_rebase", "ACTION: amend_scope")

REBASE_STORY = {
    "key": "ov-1",
    "status": "parked",
    "parked_reason": "rebase conflict: CONFLICT (content): Merge conflict in pipeline/a.py",
}

HIGH_RISK_STORY = {"key": "ov-2", "status": "parked", "parked_reason": "high risk held for human review"}


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY", raising=False)
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan_name: {}, raising=False)
    monkeypatch.setattr(triage, "_append_decision", lambda plan_name, record: None, raising=False)
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None, raising=False)
    yield


def _ruling_for(monkeypatch, raw: str) -> dict:
    monkeypatch.setattr(triage, "_invoke_overlord", lambda prompt, plan_role_config=None: raw)
    return triage.rule_on_story("plan-x", "ov-1", REBASE_STORY, "EVIDENCE")


# ---------------------------------------------------------------------------
# Prompt carries the observed class and the allowed actions
# ---------------------------------------------------------------------------


def test_prompt_names_observed_park_class_with_facts(monkeypatch):
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return RESOLVE_REBASE_RULING

    monkeypatch.setattr(triage, "_invoke_overlord", fake_invoke)
    triage.rule_on_story("plan-x", "ov-1", REBASE_STORY, "EVIDENCE")
    line = next(l for l in captured["prompt"].splitlines() if l.startswith("OBSERVED PARK CLASS: "))
    assert line.startswith("OBSERVED PARK CLASS: rebase_conflict facts=")
    assert json.loads(line.split("facts=", 1)[1])["paths"] == ["pipeline/a.py"]


def test_prompt_lists_allowed_actions_for_the_class(monkeypatch):
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return RESOLVE_REBASE_RULING

    monkeypatch.setattr(triage, "_invoke_overlord", fake_invoke)
    triage.rule_on_story("plan-x", "ov-1", REBASE_STORY, "EVIDENCE")
    line = next(l for l in captured["prompt"].splitlines() if l.startswith("ALLOWED ACTIONS: "))
    assert line == "ALLOWED ACTIONS: park_for_human, resolve_rebase"


# ---------------------------------------------------------------------------
# A disallowed ruling is replaced by park_for_human
# ---------------------------------------------------------------------------


def test_allowed_new_action_is_kept(monkeypatch):
    ruling = _ruling_for(monkeypatch, RESOLVE_REBASE_RULING)
    assert ruling["action"] == "resolve_rebase"


def test_disallowed_action_is_replaced_with_park_for_human(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", lambda prompt, plan_role_config=None: AMEND_SCOPE_RULING)
    ruling = triage.rule_on_story(
        "plan-x", "ov-2", HIGH_RISK_STORY, "EVIDENCE"
    )
    assert ruling["action"] == "park_for_human"
    assert ruling["rationale"] == "action amend_scope not allowed for park class high_risk_hold"


def test_disallowed_replacement_keeps_failed_open_false(monkeypatch):
    monkeypatch.setattr(triage, "_invoke_overlord", lambda prompt, plan_role_config=None: AMEND_SCOPE_RULING)
    ruling = triage.rule_on_story("plan-x", "ov-2", HIGH_RISK_STORY, "EVIDENCE")
    assert ruling["failed_open"] is False


# ---------------------------------------------------------------------------
# PIPELINE_TRIAGE_MODE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "shadow", "bogus"])
def test_triage_mode_defaults_to_shadow(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_TRIAGE_MODE", value)
    assert triage._triage_mode() == "shadow"


def test_triage_mode_execute_is_recognised(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    assert triage._triage_mode() == "execute"


# ---------------------------------------------------------------------------
# Shadow mode records new actions and never mutates the story
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("autonomy", ["full", "gated", "dry-run"])
def test_shadow_mode_leaves_story_untouched_for_new_action(monkeypatch, autonomy):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", autonomy)
    story = copy.deepcopy(REBASE_STORY)
    before = copy.deepcopy(story)
    ruling = {"action": "resolve_rebase", "rationale": "mechanical"}
    result = triage._apply_ruling_for_mode("plan-x", "ov-1", story, ruling, {"stories": {}})
    assert (result, story) == ("shadow", before)


def test_shadow_mode_records_the_ruling_with_mode_shadow(monkeypatch):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    records = []
    monkeypatch.setattr(triage, "_append_decision", lambda plan_name, record: records.append(record))
    story = copy.deepcopy(REBASE_STORY)
    triage._apply_ruling_for_mode("plan-x", "ov-1", story, {"action": "resolve_rebase", "rationale": "r"}, {})
    assert [(r["action"], r["mode"], r["prior_status"]) for r in records] == [
        ("resolve_rebase", "shadow", "parked"),
    ]


def test_shadow_mode_notifies_with_the_action(monkeypatch):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    messages = []
    monkeypatch.setattr(triage, "_notify_user", lambda plan_name, message, **k: messages.append(message))
    triage._apply_ruling_for_mode(
        "plan-x", "ov-1", copy.deepcopy(REBASE_STORY), {"action": "resolve_rebase", "rationale": "r"}, {}
    )
    assert messages and "resolve_rebase" in messages[0] and "shadow" in messages[0]


# ---------------------------------------------------------------------------
# Execute mode: a stub executor fails closed to park_for_human
# ---------------------------------------------------------------------------


def test_execute_mode_with_stub_executor_parks_for_human(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    story = copy.deepcopy(REBASE_STORY)
    result = triage._apply_ruling_for_mode(
        "plan-x", "ov-1", story, {"action": "approve_test_edit", "rationale": "r"}, {}
    )
    assert (result, story["status"]) == ("park_for_human", "parked")
    assert "not implemented yet" in story["parked_reason"]


def test_execute_mode_is_not_shadow_for_existing_park_action(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "full")
    result = triage._apply_ruling_for_mode(
        "plan-x", "ov-1", copy.deepcopy(REBASE_STORY), {"action": "park_for_human", "rationale": "r"}, {}
    )
    assert result == "park_for_human"
