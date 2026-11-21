"""Park options: the overlord prepares N options per parked story."""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from pipeline import park_options as po
from pipeline.settings_registry import REGISTRY

PLAN = "opt1"
SCOPE_REASON = "review rejected"
SCOPE_REVIEW = "- Blocking: pipeline/x.py: outside this story's `files` scope"

GOOD = (
    "OPTION 1 | amend_scope | Add the file | The guard names it | -\n"
    "OPTION 2 | retry | Try again | Might pass | -\n"
    "OPTION 3 | amend_scope | Add the file | duplicate pair | -\n"
    "RECOMMENDED: 2\n"
)


def _story(reason=SCOPE_REASON, status="parked"):
    return {"status": status, "parked_reason": reason, "worktree": "", "review_feedback": SCOPE_REVIEW}


@pytest.fixture
def plan(plan_dir):
    def write(stories):
        (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps({"stories": stories}))

    return write


@pytest.fixture
def overlord(monkeypatch):
    calls = []
    state = {"reply": GOOD}

    def fake(prompt, **kwargs):
        calls.append(prompt)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    monkeypatch.setattr(po, "_invoke_overlord", fake)
    monkeypatch.setattr(po, "_load_policy", lambda: "POLICY")
    monkeypatch.setattr(po, "_auto_triage_enabled", lambda: True)
    fake.calls, fake.state = calls, state
    return fake


def _records():
    return [r for r in p._store.get_decisions(PLAN) if r.get("kind") == "park_options"]


def test_scope_gate_options_come_from_the_menu_and_dedupe(plan, overlord):
    plan({"s1": _story()})
    record = po.prepare_park_options(PLAN, "s1")
    assert record["park_class"] == "scope_gate"
    assert [(o["action"], o["title"]) for o in record["options"]] == [
        ("amend_scope", "Add the file"),
        ("retry", "Try again"),
    ]
    assert record["recommended"] == "o2"


def test_prompt_asks_for_the_configured_count(plan, overlord, monkeypatch):
    plan({"s1": _story()})
    monkeypatch.setattr(po, "effective", lambda *a, **k: 2)
    po.prepare_park_options(PLAN, "s1")
    assert "exactly 2 distinct options" in overlord.calls[0]


def test_option_outside_the_menu_is_dropped_and_one_left_falls_back(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = (
        "OPTION 1 | merge_now | Merge | risky | -\n"
        "OPTION 2 | retry | Try again | ok | -\nRECOMMENDED: 2\n"
    )
    record = po.prepare_park_options(PLAN, "s1")
    assert [o["action"] for o in record["options"]] == ["retry", "amend_scope"]
    assert record["options"][0]["rationale"] == po.FALLBACK_RATIONALE
    assert record["recommended"] == "o1"


def test_overlord_failure_yields_fallback_with_retry_recommended(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = RuntimeError("down")
    record = po.prepare_park_options(PLAN, "s1")
    assert record["options"][0]["action"] == "retry"
    assert record["recommended"] == "o1"


def test_invalid_recommended_picks_first_and_only_one_is_recommended(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = GOOD.replace("RECOMMENDED: 2", "RECOMMENDED: 9")
    record = po.prepare_park_options(PLAN, "s1")
    assert record["recommended"] == "o1"
    assert "recommended" not in record["options"][0]


def test_missing_recommended_picks_first(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = GOOD.replace("RECOMMENDED: 2", "")
    assert po.prepare_park_options(PLAN, "s1")["recommended"] == "o1"


def test_record_holds_no_prompt_or_evidence_and_caps_title(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = (
        f"OPTION 1 | retry | {'t' * 300} | why | -\nOPTION 2 | amend_scope | T | why | -\nRECOMMENDED: 1\n"
    )
    record = po.prepare_park_options(PLAN, "s1")
    assert len(record["options"][0]["title"]) == 120
    assert not {"prompt", "evidence", "brief"} & set(record)
    assert "POLICY" not in json.dumps(record)
    assert record["decided_by"] == "overlord"


def test_diagnosis_kept_only_for_rebrief(plan, overlord):
    plan({"s1": {**_story("no new commit after 3 rework redispatches"), "review_feedback": ""}})
    overlord.state["reply"] = (
        "OPTION 1 | rebrief | Rewrite | why | One. Two. Three.\n"
        "OPTION 2 | retry | Retry | why | ignored\nRECOMMENDED: 1\n"
    )
    record = po.prepare_park_options(PLAN, "s1")
    by_action = {o["action"]: o["diagnosis"] for o in record["options"]}
    assert by_action == {"rebrief": "One. Two.", "retry": None}


def test_sweep_prepares_once_per_parked_reason(plan, overlord):
    plan({"s1": _story()})
    assert po.run_park_options_sweep(PLAN) == 1
    assert po.run_park_options_sweep(PLAN) == 0
    assert len(_records()) == 1
    plan({"s1": _story("review rejected again")})
    assert po.run_park_options_sweep(PLAN) == 1
    assert len(_records()) == 2


def test_sweep_handles_one_story_per_call_and_skips_non_parked(plan, overlord):
    plan({"a": _story(), "b": _story("other"), "c": _story(status="done")})
    assert po.run_park_options_sweep(PLAN) == 1
    assert po.run_park_options_sweep(PLAN) == 1
    assert po.run_park_options_sweep(PLAN) == 0
    assert sorted(r["story_key"] for r in _records()) == ["a", "b"]


def test_sweep_returns_zero_when_overlord_raises(plan, overlord):
    plan({"s1": _story()})
    overlord.state["reply"] = RuntimeError("boom")
    assert po.run_park_options_sweep(PLAN) == 1  # fallback recorded, no raise


def test_sweep_returns_zero_when_store_raises(plan, overlord, monkeypatch):
    plan({"s1": _story()})
    monkeypatch.setattr(p._store, "get_decisions", lambda *_: (_ for _ in ()).throw(OSError("x")))
    assert po.run_park_options_sweep(PLAN) == 0


def test_sweep_ignores_a_missing_plan(overlord):
    assert po.run_park_options_sweep("nope") == 0


def test_option_menu_adds_retry_and_drops_park_for_human():
    menu = po.option_menu("scope_gate")
    assert menu == ["amend_scope", "retry"]
    assert po.option_menu("high_risk_hold") == ["retry"]


@pytest.mark.parametrize("value", [1, 6, 0, True, "3"])
def test_park_option_count_refuses_out_of_range(value):
    assert not REGISTRY["park_option_count"].accepts(value)


@pytest.mark.parametrize("value", [2, 3, 5])
def test_park_option_count_accepts_range(value):
    assert REGISTRY["park_option_count"].accepts(value)


def test_sweep_is_a_noop_while_auto_triage_is_off(plan, overlord, monkeypatch):
    plan({"s1": _story()})
    monkeypatch.setattr(po, "_auto_triage_enabled", lambda: False)
    assert po.run_park_options_sweep(PLAN) == 0
    assert _records() == []
