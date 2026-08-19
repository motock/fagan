"""Tests for pipeline.triage.rule_on_story (the failure-triage ruling layer).

These tests are written FIRST (TDD). They monkeypatch the overlord/policy/
persistence helpers on `pipeline.triage` so no real backend call is ever made.
They must fail for the right reason - a missing `rule_on_story` attribute or
failing assertion - until the implementation exists.
"""

import logging

import pytest

import pipeline.triage as triage


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

WELL_FORMED_RULING = (
    "RULING: the build failed because of a missing import\n"
    "TIER: hard\n"
    "RISK: high\n"
    "RATIONALE: the agent never added the import the story required\n"
    "NOTIFY_USER: yes\n"
    "ACTION: escalate_model\n"
)

EVIDENCE = "=== FAILURE EVIDENCE ===\nbuild failed: ModuleNotFoundError: No module named 'foo'\n"


@pytest.fixture(autouse=True)
def _reset_triage_mocks(monkeypatch):
    """Ensure each test starts from a clean, deterministic set of stubs."""
    # Default stubs; individual tests override as needed.
    monkeypatch.setattr(triage, "_load_policy", lambda: "GLOBAL POLICY TEXT")
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan_name: {"overlord": {"provider": "ollama"}})
    monkeypatch.setattr(triage, "_append_decision", lambda plan_name, record: None)
    yield


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_parsed_action_and_failed_open_false():
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        captured["plan_role_config"] = plan_role_config
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke
    append_calls = []
    triage._append_decision = lambda plan_name, record: append_calls.append((plan_name, record))

    result = triage.rule_on_story("plan-x", "story-1", {"key": "story-1"}, EVIDENCE)

    assert result["action"] == "escalate_model"
    assert result["failed_open"] is False
    assert result["ruling"] == "the build failed because of a missing import"
    assert result["tier"] == "hard"
    assert result["risk"] == "high"
    assert result["notify_user"] is True

    # _append_decision called exactly once with the right shape
    assert len(append_calls) == 1
    plan_name, record = append_calls[0]
    assert plan_name == "plan-x"
    assert record["story_key"] == "story-1"
    assert record["decided_by"] == "overlord-triage"
    assert record["question"] == "failure triage"
    assert record["action"] == "escalate_model"
    assert "decided_at" in record


def test_invoke_overlord_called_with_exactly_prompt_and_plan_role_config_no_model_kw():
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["args"] = (prompt,)
        captured["kwargs"] = {"plan_role_config": plan_role_config}
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke
    role_cfg = {"overlord": {"provider": "ollama", "model": "glm"}}
    triage._plan_role_config = lambda plan_name: role_cfg

    triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    # Exactly one positional arg (the prompt) and plan_role_config kwarg.
    assert captured["args"] == (captured["args"][0],)
    assert len(captured["args"]) == 1
    assert captured["kwargs"]["plan_role_config"] is role_cfg
    # No 'model' keyword may be passed.
    assert "model" not in captured["kwargs"]


def test_prompt_contains_evidence_and_action_substring():
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke

    triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert EVIDENCE in captured["prompt"]
    assert "ACTION" in captured["prompt"]


def test_prompt_contains_policy_text():
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke
    triage._load_policy = lambda: "UNIQUE_POLICY_MARKER_42"

    triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert "UNIQUE_POLICY_MARKER_42" in captured["prompt"]


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


def test_invoke_overlord_runtime_error_fails_open_and_still_appends():
    def fake_invoke(prompt, plan_role_config=None):
        raise RuntimeError("connection reset")

    triage._invoke_overlord = fake_invoke
    append_calls = []
    triage._append_decision = lambda plan_name, record: append_calls.append((plan_name, record))

    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert result["action"] == "park_for_human"
    assert result["failed_open"] is True
    assert result["notify_user"] is True
    assert result["ruling"] == ""
    assert result["tier"] == ""
    assert result["risk"] == ""
    # Still appended exactly once (audit trail on the failed-open path too).
    assert len(append_calls) == 1
    plan_name, record = append_calls[0]
    assert plan_name == "plan-x"
    assert record["story_key"] == "story-1"
    assert record["decided_by"] == "overlord-triage"
    assert record["action"] == "park_for_human"
    assert record["failed_open"] is True


def test_failed_open_rationale_names_exception_type_not_message():
    def fake_invoke(prompt, plan_role_config=None):
        raise RuntimeError("connection reset")

    triage._invoke_overlord = fake_invoke

    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert "RuntimeError" in result["rationale"]
    assert "connection reset" not in result["rationale"]


def test_load_policy_oserror_fails_open():
    def fake_invoke(prompt, plan_role_config=None):
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke

    def boom():
        raise OSError("disk on fire")

    triage._load_policy = boom

    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert result["action"] == "park_for_human"
    assert result["failed_open"] is True
    assert "OSError" in result["rationale"]


def test_invoke_overlord_empty_string_returns_park():
    triage._invoke_overlord = lambda prompt, plan_role_config=None: ""
    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)
    assert result["action"] == "park_for_human"


def test_invoke_overlord_none_returns_park_no_typeerror():
    triage._invoke_overlord = lambda prompt, plan_role_config=None: None
    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)
    assert result["action"] == "park_for_human"


def test_invoke_overlord_unstructured_prose_returns_park():
    triage._invoke_overlord = lambda prompt, plan_role_config=None: (
        "I think the build is broken and someone should look at it eventually."
    )
    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)
    assert result["action"] == "park_for_human"


# ---------------------------------------------------------------------------
# Persistence robustness
# ---------------------------------------------------------------------------


def test_append_decision_oserror_does_not_propagate_and_ruling_still_returned():
    def fake_invoke(prompt, plan_role_config=None):
        return WELL_FORMED_RULING

    triage._invoke_overlord = fake_invoke

    def boom(plan_name, record):
        raise OSError("disk full")

    triage._append_decision = boom

    # Must not raise.
    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    assert result["action"] == "escalate_model"
    assert result["failed_open"] is False


def test_append_decision_oserror_on_failed_open_path_does_not_propagate():
    def fake_invoke(prompt, plan_role_config=None):
        raise RuntimeError("connection reset")

    triage._invoke_overlord = fake_invoke

    def boom(plan_name, record):
        raise OSError("disk full")

    triage._append_decision = boom

    # Must not raise even on the failed-open path.
    result = triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)
    assert result["action"] == "park_for_human"
    assert result["failed_open"] is True


# ---------------------------------------------------------------------------
# Logging discipline: only exception TYPE, never str(exc)
# ---------------------------------------------------------------------------


def test_fail_open_logs_warning_with_exception_type_only(caplog):
    def fake_invoke(prompt, plan_role_config=None):
        raise RuntimeError("connection reset with secret endpoint https://internal:token@host")

    triage._invoke_overlord = fake_invoke

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        triage.rule_on_story("plan-x", "story-1", {}, EVIDENCE)

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in joined
    # The sensitive message body must NOT appear in any log record.
    assert "secret endpoint" not in joined
    assert "connection reset with secret endpoint" not in joined


# ---------------------------------------------------------------------------
# __all__ membership
# ---------------------------------------------------------------------------


def test_rule_on_story_exported_in_all():
    assert "rule_on_story" in triage.__all__


# ---------------------------------------------------------------------------
# Module-level import discipline: names imported on pipeline.triage
# ---------------------------------------------------------------------------


def test_required_names_imported_on_triage_module():
    # The implementation must import these NAMES (not just the modules) so
    # tests can patch them on pipeline.triage.
    for name in ("_invoke_overlord", "_load_policy", "_parse_ruling", "_append_decision", "_plan_role_config"):
        assert hasattr(triage, name), f"pipeline.triage must expose {name} for monkeypatching"