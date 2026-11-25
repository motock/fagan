"""``record_decision`` on a ruled story links the ruling and records the reasoning.

A judgment recorded against a story with an overlord ruling carries the ruling's
``decided_at`` as ``ruling_ref`` in its decision record. When that ruling has no
human resolution yet, the record also writes one, with the decision class mapped
to a human action and the rationale carried over, redacted and capped at 500
characters. A second judgment on the same ruling adds no second resolution. A
story with no ruling records the judgment exactly as before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.identity import Principal
from pipeline import decision_record_tool as tool
from pipeline import decision_records, paths, persistence
from pipeline.actor import bind_principal

PLAN = "plan-ruled"
STORY = "s1"
DECIDED = "2026-11-20T10:00:00+00:00"
HUMAN = Principal(subject="human:ana", auth_method="oidc", principal_type="human")
EVIDENCE = [{"kind": "test_run", "ref": "pytest -q tests/unit/test_x.py", "sha256": ""}]

MANIFEST: dict[str, Any] = {
    "team": "team-alpha",
    "owner": HUMAN.subject,
    "delegates": [],
    "stories": {STORY: {"key": STORY, "title": "a story", "status": "parked"}},
}


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        tool, "_manifest", lambda plan_name: dict(MANIFEST) if plan_name == PLAN else {}
    )
    return MANIFEST


def _call(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "plan_name": PLAN,
        "story_key": STORY,
        "decision_class": "scope_widen",
        "question": "widen the story's scope?",
        "ruling": "widen",
        "rationale": "the brief names the file and the test already covers it",
        "evidence": [dict(item) for item in EVIDENCE],
    }
    arguments.update(overrides)
    with bind_principal(HUMAN):
        return tool.record_decision(**arguments)


def _seed_ruling(decided_at: str = DECIDED) -> None:
    persistence._append_decision(
        PLAN,
        {
            "story_key": STORY,
            "question": "failure triage",
            "action": "park_for_human",
            "park_class": "rework",
            "decided_by": "overlord-triage",
            "decided_at": decided_at,
        },
    )


def _legacy_decisions() -> list[dict[str, Any]]:
    path = persistence._decisions_path(PLAN)
    return json.loads(path.read_text()) if path.exists() else []


def _resolutions() -> list[dict[str, Any]]:
    return [r for r in _legacy_decisions() if r.get("question") == "human resolution"]


def _records() -> list[dict[str, Any]]:
    return decision_records.read_decision_records(PLAN)


# --------------------------------------------------------------------------
# A ruled story: the decision record links the ruling
# --------------------------------------------------------------------------


def test_judgment_on_a_ruled_story_writes_the_ruling_ref(manifest) -> None:
    _seed_ruling()

    result = _call()

    assert result["recorded"] is True
    assert _records()[0]["ruling_ref"] == DECIDED


def test_judgment_on_a_ruled_story_records_one_resolution_with_the_rationale(manifest) -> None:
    _seed_ruling()

    _call(rationale="the brief names the file and the test already covers it")

    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["ruling_ref"] == DECIDED
    assert resolutions[0]["human_action"] == "amend_scope"
    assert resolutions[0]["rationale"] == "the brief names the file and the test already covers it"
    assert resolutions[0]["decided_by"] == HUMAN.subject


@pytest.mark.parametrize(
    ("decision_class", "human_action"),
    [
        ("scope_widen", "amend_scope"),
        ("existing_test_edit", "approve_test_edit"),
        ("high_risk_merge", "merge_held"),
        ("override_false_positive_verdict", "other"),
        ("triage", "other"),
        ("other", "other"),
    ],
)
def test_decision_class_maps_to_its_human_action(manifest, decision_class, human_action) -> None:
    _seed_ruling()

    _call(decision_class=decision_class)

    assert [r["human_action"] for r in _resolutions()] == [human_action]


def test_long_rationale_is_truncated_to_500_characters_in_the_resolution(manifest) -> None:
    _seed_ruling()

    _call(rationale="R" * 900)

    assert _resolutions()[0]["rationale"] == "R" * 500


def test_rationale_secret_is_redacted_in_the_resolution(manifest) -> None:
    _seed_ruling()

    _call(rationale="swap the key, password=hunter2hunter2 was pasted into the brief")

    rationale = _resolutions()[0]["rationale"]
    assert "hunter2hunter2" not in rationale
    assert "password=" in rationale


def test_second_judgment_on_the_same_ruling_adds_no_second_resolution(manifest) -> None:
    _seed_ruling()

    first = _call(rationale="first reasoning")
    second = _call(rationale="a richer second reasoning")

    assert first["recorded"] is True
    assert second["recorded"] is True
    assert len(_records()) == 2
    assert [r["ruling_ref"] for r in _records()] == [DECIDED, DECIDED]
    resolutions = _resolutions()
    assert len(resolutions) == 1
    assert resolutions[0]["rationale"] == "first reasoning"


def test_a_new_ruling_is_answered_by_a_new_resolution(manifest) -> None:
    _seed_ruling("2026-11-20T10:00:00+00:00")
    _call()
    _seed_ruling("2026-11-21T10:00:00+00:00")

    _call()

    assert [r["ruling_ref"] for r in _resolutions()] == [
        "2026-11-20T10:00:00+00:00",
        "2026-11-21T10:00:00+00:00",
    ]


# --------------------------------------------------------------------------
# A story with no ruling: recorded as before, nothing linked
# --------------------------------------------------------------------------


def test_judgment_without_a_ruling_has_no_ruling_ref_and_no_resolution(manifest) -> None:
    result = _call()

    assert result["recorded"] is True
    record = _records()[0]
    assert "ruling_ref" not in record
    assert _resolutions() == []


def test_judgment_on_a_ruled_story_with_a_refused_ruling_text_writes_nothing(manifest) -> None:
    _seed_ruling()

    result = _call(ruling="")

    assert result == {"recorded": False, "error": "empty_ruling"}
    assert _resolutions() == []
