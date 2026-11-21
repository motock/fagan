"""A park_option_chosen record carries the park class of the options it chose from."""

from __future__ import annotations

import json

import pipeline.server as p
from pipeline import park_option_choice as poc
from pipeline.park_options import _digest

PLAN = "pcc1"
REASON = "review rejected"


def _options(park_class="scope_gate"):
    return {
        "kind": "park_options",
        "story_key": "s1",
        "park_class": park_class,
        "parked_reason_digest": _digest(REASON),
        "options": [
            {"id": "o1", "action": "amend_scope", "title": "Add file", "rationale": "guard names it", "diagnosis": None},
            {"id": "o2", "action": "retry", "title": "Retry", "rationale": "may pass", "diagnosis": None},
        ],
        "recommended": "o2",
        "decided_by": "overlord",
        "decided_at": "2026-11-20T00:00:00+00:00",
    }


def _write_parked_story(plan_dir):
    story = {"status": "parked", "parked_reason": REASON, "rework_attempts": 3}
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps({"stories": {"s1": story}}))


def _chosen_records():
    return [r for r in p._store.get_decisions(PLAN) if r.get("kind") == "park_option_chosen"]


def test_chosen_record_carries_the_park_class_of_the_options(plan_dir):
    _write_parked_story(plan_dir)
    p._store.append_decision(PLAN, _options(park_class="scope_gate"))

    assert poc.choose_park_option(PLAN, "s1", "o2")["ok"] is True

    assert _chosen_records()[0]["park_class"] == "scope_gate"


def test_chosen_record_carries_the_class_when_the_choice_is_not_recommended(plan_dir, monkeypatch):
    _write_parked_story(plan_dir)
    p._store.append_decision(PLAN, _options(park_class="rebase_conflict"))
    monkeypatch.setattr(poc, "execute_ruling", lambda *args: "amend_scope")

    assert poc.choose_park_option(PLAN, "s1", "o1")["ok"] is True

    chosen = _chosen_records()[0]
    assert chosen["chose_recommended"] is False
    assert chosen["park_class"] == "rebase_conflict"
