"""Wiring of the ``amend_scope`` triage action: dispatcher, autonomy, evidence, policy."""

from pathlib import Path

import pytest

from pipeline import server, triage
from pipeline.parsers import _parse_ruling

PLAN = "plan-a"
KEY = "HR-X"
_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"


def _story() -> dict:
    return {
        "status": "parked",
        "parked_reason": "scope conflict",
        "files": ["pipeline/foo.py"],
        "scope_conflict": {
            "paths": ["pipeline/bar.py"],
            "reasons": {"pipeline/bar.py": "brief requires editing bar"},
        },
    }


@pytest.fixture
def records(monkeypatch):
    recorded = []
    monkeypatch.setattr(triage, "_append_decision", lambda plan, rec: recorded.append(rec))
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
    return recorded


def test_amend_scope_ruling_reaches_executor_with_arguments(monkeypatch):
    seen = {}

    def fake(*args):
        seen["args"] = args
        return "amend_scope"

    monkeypatch.setattr(triage, "_execute_amend_scope", fake)
    story, manifest = _story(), {"stories": {}}
    ruling = {"action": "amend_scope", "rationale": "needed"}

    result = triage.execute_ruling(PLAN, KEY, story, ruling, manifest, "m.json")

    assert result == "amend_scope"
    assert seen["args"] == (PLAN, KEY, story, ruling, manifest, "m.json")


def test_dry_run_does_not_execute_amend_scope(monkeypatch, records):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", "dry-run")
    monkeypatch.setattr(
        triage, "_execute_amend_scope", lambda *a: pytest.fail("must not execute")
    )
    story = _story()

    result = triage._apply_ruling_for_mode(
        PLAN, KEY, story, {"action": "amend_scope", "rationale": "x"}, {}, "m.json"
    )

    assert result == "dry-run"
    assert story["files"] == ["pipeline/foo.py"]


def test_unknown_action_still_parks(monkeypatch):
    parked = []
    monkeypatch.setattr(triage, "_park", lambda *a: parked.append(a) or "parked")

    result = triage.execute_ruling(
        PLAN, KEY, _story(), {"action": "frobnicate", "rationale": "x"}, {}, "m.json"
    )

    assert result == "parked"
    assert "unhandled ruling action 'frobnicate'" in parked[0][3]


def test_parser_accepts_amend_scope_action():
    assert _parse_ruling("ACTION: amend_scope\nRATIONALE: x")["action"] == "amend_scope"


def test_evidence_includes_recorded_scope_conflict_paths():
    text = triage.collect_triage_evidence("/nonexistent/worktree", _story())

    assert "pipeline/bar.py" in text
    assert "brief requires editing bar" in text


@pytest.mark.parametrize("mode", ["gated", "full"])
def test_amend_scope_writes_exactly_one_record_with_mode_and_paths(
    monkeypatch, records, tmp_path, mode
):
    monkeypatch.setattr(server, "PIPELINE_AUTONOMY", mode)
    monkeypatch.setattr(
        "pipeline.triage_scope_actions._is_green_at_head", lambda story: False
    )
    manifest_path = tmp_path / "m.json"
    story = _story()
    manifest = {"stories": {KEY: story}}

    result = triage._apply_ruling_for_mode(
        PLAN, KEY, story, {"action": "amend_scope", "rationale": "x"}, manifest,
        manifest_path,
    )

    assert result == "amend_scope"
    assert len(records) == 1
    assert records[0]["mode"] == mode
    assert records[0]["amended_paths"] == ["pipeline/bar.py"]
    assert "_amended_paths" not in story


def test_policy_lists_amend_scope_once_in_action_list():
    section = _POLICY.read_text().partition("## Failure triage")[2]
    action_list = section.partition("### Parked-story resolution")[0]

    assert action_list.count("- `amend_scope`") == 1


def test_policy_output_contract_and_resolution_table_name_amend_scope():
    text = _POLICY.read_text()

    assert "patch_acceptance | amend_scope\n" in text
    assert "| `amend_scope` |" in text
