"""Park classes for the parks the overlord sees today (ov-01).

Each fixture is the shape a real park carries: the field the overlord's own
writer sets (``park_reason`` for a plan-conflict park, ``scope_conflict`` for an
agent's SCOPE-CONFLICT report) and the test-run tail the triage loop passes in.
Fixtures live here rather than under ``tests/fixtures/triage_park_cases/``,
whose file count the corpus test pins.
"""

import pytest

from pipeline.triage_park_classes import (
    ALLOWED_ACTIONS,
    ParkClass,
    check_ruling_allowed,
    classify_park,
)

_PLAN_CONFLICT_REASON = (
    "plan conflict: pre-existing tests tests/unit/test_pgs_a.py, tests/unit/test_pgs_b.py "
    "contradict the brief - overlord parked the story"
)
_MISSING_PYTEST_TAIL = (
    "Traceback (most recent call last):\n"
    "  File \"/usr/lib/python3.11/subprocess.py\", line 1866, in _execute_child\n"
    "FileNotFoundError: [Errno 2] No such file or directory: 'pytest'\n"
)


# ---------------------------------------------------------------------------
# Plan-conflict park: the overlord writes park_reason, not parked_reason
# ---------------------------------------------------------------------------


def test_plan_conflict_park_reason_classifies_as_brief_conflict():
    park = classify_park({"parked_reason": None, "park_reason": _PLAN_CONFLICT_REASON}, {})
    assert (park.name, park.facts["tests"]) == (
        "brief_conflicts_existing_test",
        ["tests/unit/test_pgs_a.py", "tests/unit/test_pgs_b.py"],
    )


def test_parked_reason_takes_precedence_over_park_reason():
    park = classify_park(
        {"parked_reason": "no new commit after 2 rework redispatches", "park_reason": _PLAN_CONFLICT_REASON},
        {},
    )
    assert park.name == "no_commit_after_rework"


def test_empty_parked_reason_falls_back_to_park_reason():
    park = classify_park({"parked_reason": "", "park_reason": _PLAN_CONFLICT_REASON}, {})
    assert park.name == "brief_conflicts_existing_test"


def test_plan_conflict_prefix_must_name_pre_existing_tests():
    park = classify_park({"park_reason": "plan conflict: the grade is ambiguous"}, {})
    assert park.name == "unknown"


def test_park_reason_mid_sentence_plan_conflict_is_not_brief_conflict():
    park = classify_park(
        {"park_reason": "overlord note: plan conflict: pre-existing tests tests/unit/test_a.py"},
        {},
    )
    assert park.name == "unknown"


def test_non_string_park_reason_is_unknown_without_raising():
    assert classify_park({"park_reason": ["plan conflict: pre-existing tests"]}, {}).name == "unknown"


# ---------------------------------------------------------------------------
# Agent-recorded scope conflict: the story's scope_conflict field
# ---------------------------------------------------------------------------


def test_scope_conflict_field_classifies_as_scope_gate_with_its_paths():
    story = {
        "status": "parked",
        "parked_reason": "scope conflict: pipeline/outbox_health.py",
        "scope_conflict": {
            "paths": ["pipeline/outbox_health.py"],
            "reasons": {"pipeline/outbox_health.py": "the guard test reads it"},
            "recorded_at": "2026-11-14T09:00:00+00:00",
        },
    }
    park = classify_park(story, {})
    assert (park.name, park.facts) == ("scope_gate", {"paths": ["pipeline/outbox_health.py"]})


def test_scope_conflict_paths_and_review_paths_are_merged_without_duplicates():
    story = {"scope_conflict": {"paths": ["pipeline/a.py", "pipeline/b.py"]}}
    evidence = {"review_feedback": "- Blocking: pipeline/b.py: outside this story's `files` scope"}
    park = classify_park(story, evidence)
    assert park.facts["paths"] == ["pipeline/a.py", "pipeline/b.py"]


def test_empty_scope_conflict_paths_is_not_a_scope_gate():
    assert classify_park({"scope_conflict": {"paths": []}}, {}).name == "unknown"


def test_scope_conflict_that_is_not_a_mapping_is_unknown_without_raising():
    assert classify_park({"scope_conflict": "pipeline/a.py"}, {}).name == "unknown"


def test_scope_conflict_with_non_string_paths_is_unknown_without_raising():
    assert classify_park({"scope_conflict": {"paths": [3, None]}}, {}).name == "unknown"


# ---------------------------------------------------------------------------
# Missing tool binary: FileNotFoundError from the test run
# ---------------------------------------------------------------------------


def test_missing_tool_binary_in_test_output_classifies_as_env_tool_missing():
    park = classify_park({"status": "parked"}, {"test_output": _MISSING_PYTEST_TAIL})
    assert (park.name, park.facts) == ("env_tool_missing", {"tool": "pytest"})


def test_missing_path_not_a_bare_tool_name_is_unknown():
    tail = "FileNotFoundError: [Errno 2] No such file or directory: 'tests/data/input.json'\n"
    assert classify_park({}, {"test_output": tail}).name == "unknown"


def test_missing_absolute_binary_path_is_unknown():
    tail = "FileNotFoundError: [Errno 2] No such file or directory: '/usr/bin/pytest'\n"
    assert classify_park({}, {"test_output": tail}).name == "unknown"


def test_file_not_found_without_errno_text_is_unknown():
    assert classify_park({}, {"test_output": "FileNotFoundError: 'pytest'\n"}).name == "unknown"


# ---------------------------------------------------------------------------
# Priority: new specific checks do not displace the guard or rebase classes
# ---------------------------------------------------------------------------


def test_guard_demands_path_outranks_scope_conflict_field():
    story = {"files": [], "scope_conflict": {"paths": ["pipeline/b.py"]}}
    evidence = {"test_output": "stale entry, remove it: pipeline/a.py X\n"}
    assert classify_park(story, evidence).name == "guard_demands_path"


def test_rebase_conflict_outranks_plan_conflict_text():
    story = {"parked_reason": "rebase conflict: CONFLICT (content): Merge conflict in pipeline/a.py",
             "park_reason": _PLAN_CONFLICT_REASON}
    assert classify_park(story, {}).name == "rebase_conflict"


# ---------------------------------------------------------------------------
# Allowed actions for the new classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["amend_scope", "approve_test_edit", "rebrief", "park_for_human"])
def test_brief_conflict_allows_its_four_actions(action):
    assert check_ruling_allowed(ParkClass("brief_conflicts_existing_test", {}), action)


@pytest.mark.parametrize("action", ["mark_done", "resolve_rebase", "repo_issue", "escalate_model"])
def test_brief_conflict_refuses_other_actions(action):
    assert not check_ruling_allowed(ParkClass("brief_conflicts_existing_test", {}), action)


@pytest.mark.parametrize("action", ["repo_issue", "park_for_human"])
def test_env_tool_missing_allows_its_two_actions(action):
    assert check_ruling_allowed(ParkClass("env_tool_missing", {}), action)


@pytest.mark.parametrize("action", ["rebrief", "amend_scope", "retry_transient", "mark_done"])
def test_env_tool_missing_refuses_other_actions(action):
    assert not check_ruling_allowed(ParkClass("env_tool_missing", {}), action)


def test_new_classes_have_exactly_the_specified_action_sets():
    assert ALLOWED_ACTIONS["brief_conflicts_existing_test"] == frozenset(
        {"amend_scope", "approve_test_edit", "rebrief", "park_for_human"}
    )
    assert ALLOWED_ACTIONS["env_tool_missing"] == frozenset({"repo_issue", "park_for_human"})


# ---------------------------------------------------------------------------
# Unchanged path: a story with none of the new signals classifies as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("story", "evidence", "expected"),
    [
        ({"parked_reason": "rebase conflict: CONFLICT (content): Merge conflict in pipeline/a.py"}, {}, "rebase_conflict"),
        ({"parked_reason": "plan lock contended; retry next tick"}, {}, "transient_bookkeeping"),
        ({"parked_reason": "high risk held for human review"}, {}, "high_risk_hold"),
        ({"parked_reason": "no new commit after 2 rework redispatches"}, {}, "no_commit_after_rework"),
        ({"parked_reason": "something the overlord has never seen"}, {}, "unknown"),
        ({}, {}, "unknown"),
    ],
)
def test_legacy_park_without_new_signals_keeps_its_class(story, evidence, expected):
    assert classify_park(story, evidence).name == expected


def test_existing_class_actions_are_unchanged():
    assert ALLOWED_ACTIONS["scope_gate"] == frozenset({"amend_scope", "park_for_human"})
    assert ALLOWED_ACTIONS["rebase_conflict"] == frozenset({"resolve_rebase", "park_for_human"})
    assert ALLOWED_ACTIONS["unknown"] == frozenset({
        "escalate_model", "split_story", "repo_issue", "park_for_human",
        "mark_done", "patch_acceptance", "amend_scope",
    })


# ---------------------------------------------------------------------------
# Agent failed after pushing: status 'failed', commits on the branch, no verdict
# ---------------------------------------------------------------------------


def test_failed_story_with_pushed_head_and_no_verdict_is_agent_failed_after_push():
    story = {"status": "failed", "last_commit": "a1b2c3d", "review_verdict_sha": "", "last_reviewed_sha": ""}
    assert classify_park(story, {}).name == "agent_failed_after_push"


def test_verdict_for_an_older_head_does_not_cover_the_pushed_head():
    story = {"status": "failed", "last_commit": "a1b2c3d", "review_verdict_sha": "0f0e0d0"}
    assert classify_park(story, {}).name == "agent_failed_after_push"


def test_failed_story_without_commits_is_not_agent_failed_after_push():
    assert classify_park({"status": "failed", "last_commit": ""}, {}).name == "unknown"
    assert classify_park({"status": "failed"}, {}).name == "unknown"


def test_failed_story_with_review_verdict_for_its_head_is_not_agent_failed_after_push():
    story = {"status": "failed", "last_commit": "a1b2c3d", "review_verdict_sha": "a1b2c3d"}
    assert classify_park(story, {}).name == "unknown"


def test_failed_story_reviewed_at_its_head_is_not_agent_failed_after_push():
    story = {"status": "failed", "last_commit": "a1b2c3d", "last_reviewed_sha": "a1b2c3d"}
    assert classify_park(story, {}).name == "unknown"


@pytest.mark.parametrize("status", ["parked", "running", "interrupted", "done", None])
def test_non_failed_story_with_pushed_head_is_not_agent_failed_after_push(status):
    story = {"status": status, "last_commit": "a1b2c3d", "review_verdict_sha": ""}
    assert classify_park(story, {}).name == "unknown"


def test_non_string_last_commit_is_unknown_without_raising():
    assert classify_park({"status": "failed", "last_commit": ["a1b2c3d"]}, {}).name == "unknown"


def test_agent_failed_after_push_allows_resume_review_and_park_for_human():
    assert ALLOWED_ACTIONS["agent_failed_after_push"] == frozenset({"resume_review", "park_for_human"})


@pytest.mark.parametrize("action", ["amend_scope", "rebrief", "mark_done", "repo_issue"])
def test_agent_failed_after_push_refuses_other_actions(action):
    assert not check_ruling_allowed(ParkClass("agent_failed_after_push", {}), action)


def test_agent_failed_after_push_allows_resume_review_on_check():
    assert check_ruling_allowed(ParkClass("agent_failed_after_push", {}), "resume_review")
