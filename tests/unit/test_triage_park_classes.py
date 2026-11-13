"""Tests for pipeline.triage_park_classes: the park-class vocabulary.

Written FIRST (TDD). The corpus under tests/fixtures/triage_park_cases/ holds
one JSON file per real 2026-11-13 park, recorded verbatim. Each file asserts
the class the classifier must return and the facts it must carry.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from pipeline.triage_park_classes import (
    ALLOWED_ACTIONS,
    ParkClass,
    check_ruling_allowed,
    classify_park,
)

_CORPUS = sorted(
    (Path(__file__).resolve().parents[1] / "fixtures" / "triage_park_cases").glob("*.json")
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Corpus: one parametrised case per recorded park
# ---------------------------------------------------------------------------


def test_corpus_has_one_file_per_recorded_case():
    assert len(_CORPUS) == 9


@pytest.mark.parametrize("case_path", _CORPUS, ids=lambda p: p.stem)
def test_corpus_case_classifies_to_expected_class(case_path):
    case = _load(case_path)
    park = classify_park(case["story"], case["evidence"])
    assert park.name == case["expected"]["name"]


@pytest.mark.parametrize("case_path", _CORPUS, ids=lambda p: p.stem)
def test_corpus_case_carries_expected_facts(case_path):
    case = _load(case_path)
    park = classify_park(case["story"], case["evidence"])
    expected = case["expected"]["facts"]
    mismatched = {k: (park.facts.get(k), v) for k, v in expected.items() if park.facts.get(k) != v}
    assert mismatched == {}


# ---------------------------------------------------------------------------
# Per-class signals (positive cases beyond the corpus)
# ---------------------------------------------------------------------------


def test_rebase_conflict_kind_add_add_is_recognised():
    park = classify_park(
        {},
        {"parked_reason": "rebase conflict: CONFLICT (add/add): Merge conflict in pipeline/new.py"},
    )
    assert (park.name, park.facts["kind"], park.facts["paths"]) == (
        "rebase_conflict",
        "add_add",
        ["pipeline/new.py"],
    )


def test_rebase_conflict_unrecognised_kind_is_other():
    park = classify_park(
        {},
        {"parked_reason": "rebase conflict: CONFLICT (rename/delete): pipeline/a.py renamed"},
    )
    assert (park.name, park.facts["kind"]) == ("rebase_conflict", "other")


def test_ci_rework_flag_alone_classifies_as_ci_red():
    park = classify_park({"ci_rework": True}, {})
    assert park.name == "ci_red_after_rework"


def test_merge_park_pr_checks_not_fail_does_not_classify_as_ci_red():
    park = classify_park({}, {"merge_park_evidence": {"pr_checks": {"state": "pending"}}})
    assert park.name == "unknown"


def test_no_commit_reason_records_rework_count():
    park = classify_park({}, {"parked_reason": "no new commit after 3 rework redispatches - stopped."})
    assert (park.name, park.facts["n"]) == ("no_commit_after_rework", 3)


def test_transient_plan_lock_contended_classifies():
    park = classify_park({}, {"parked_reason": "plan lock contended; retry next tick"})
    assert park.name == "transient_bookkeeping"


def test_transient_unchanged_since_last_review_classifies():
    park = classify_park({}, {"parked_reason": "unchanged_since_last_review"})
    assert park.name == "transient_bookkeeping"


# ---------------------------------------------------------------------------
# Priority when several signals match
# ---------------------------------------------------------------------------


def test_guard_demands_path_outranks_scope_gate():
    evidence = {
        "review_feedback": "- Blocking: pipeline/a.py: outside this story's `files` scope",
        "test_output": "stale entry, remove it: pipeline/b.py X\n",
    }
    assert classify_park({"files": []}, evidence).name == "guard_demands_path"


def test_scope_gate_outranks_rebase_conflict():
    evidence = {
        "parked_reason": "rebase conflict: CONFLICT (content): Merge conflict in pipeline/a.py",
        "review_feedback": "- Blocking: pipeline/b.py: outside this story's `files` scope",
    }
    assert classify_park({}, evidence).name == "scope_gate"


def test_ci_red_outranks_transient_and_high_risk():
    park = classify_park(
        {"ci_rework": True},
        {"parked_reason": "high risk held for human review"},
    )
    assert park.name == "ci_red_after_rework"


def test_transient_outranks_high_risk_hold():
    park = classify_park(
        {},
        {"parked_reason": "high risk held for human review; plan lock contended"},
    )
    assert park.name == "transient_bookkeeping"


# ---------------------------------------------------------------------------
# Negative cases: must return unknown, never raise
# ---------------------------------------------------------------------------


def test_empty_story_and_evidence_is_unknown():
    park = classify_park({}, {})
    assert (park.name, park.facts) == ("unknown", {})


def test_story_with_only_status_is_unknown():
    assert classify_park({"status": "parked"}, {}).name == "unknown"


def test_non_string_evidence_values_are_unknown_without_raising():
    evidence = {
        "parked_reason": 42,
        "review_feedback": ["- Blocking: pipeline/a.py: outside this story's `files` scope"],
        "test_output": None,
        "files": "pipeline/a.py",
        "merge_park_evidence": "fail",
    }
    assert classify_park({}, evidence).name == "unknown"


def test_rebase_words_mid_sentence_do_not_classify_as_rebase_conflict():
    park = classify_park(
        {},
        {"parked_reason": "parked after an earlier rebase conflict: CONFLICT (content): Merge conflict in x.py"},
    )
    assert park.name == "unknown"


def test_non_dict_inputs_are_unknown_without_raising():
    assert classify_park(None, None).name == "unknown"
    assert classify_park("story", ["evidence"]).name == "unknown"


def test_park_class_is_frozen():
    park = ParkClass(name="unknown", facts={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        park.name = "scope_gate"


# ---------------------------------------------------------------------------
# Allowed actions
# ---------------------------------------------------------------------------


def test_unknown_class_allows_the_existing_seven_actions():
    unknown = ParkClass(name="unknown", facts={})
    existing = (
        "escalate_model", "split_story", "repo_issue", "park_for_human",
        "mark_done", "patch_acceptance", "amend_scope",
    )
    assert all(check_ruling_allowed(unknown, action) for action in existing)


def test_unknown_class_rejects_the_new_actions():
    unknown = ParkClass(name="unknown", facts={})
    assert not check_ruling_allowed(unknown, "resolve_rebase")


def test_high_risk_hold_allows_only_park_for_human():
    hold = ParkClass(name="high_risk_hold", facts={})
    assert ALLOWED_ACTIONS["high_risk_hold"] == frozenset({"park_for_human"})
    assert check_ruling_allowed(hold, "park_for_human")
    assert not check_ruling_allowed(hold, "amend_scope")


def test_rebase_conflict_allows_resolve_rebase_and_park():
    rebase = ParkClass(name="rebase_conflict", facts={})
    assert check_ruling_allowed(rebase, "resolve_rebase")
    assert check_ruling_allowed(rebase, "park_for_human")
    assert not check_ruling_allowed(rebase, "mark_done")


def test_unrecognised_class_name_fails_closed():
    assert not check_ruling_allowed(ParkClass(name="made_up", facts={}), "park_for_human")


def test_every_non_unknown_class_allows_park_for_human():
    for name, actions in ALLOWED_ACTIONS.items():
        assert "park_for_human" in actions, name
