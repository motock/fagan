"""The oracle-green auto-commit subject is derived from the story summary."""
# The helpers module sets LOCAL_AGENT_MODEL at import, which the config module
# requires, so it must be imported before the config module.
from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401, I001
    _isolate_environ,
    load_oracle_module_with_env,
)
from scripts.local_agent_oracle_config import green_commit_subject

PLACEHOLDER = "feat: implement task (acceptance oracle green)"


def test_should_fall_back_to_placeholder_when_summary_empty():
    assert green_commit_subject("") == PLACEHOLDER


def test_should_fall_back_to_placeholder_when_summary_is_only_whitespace():
    assert green_commit_subject("  \t ") == PLACEHOLDER


def test_should_prefix_summary_with_feat():
    summary = "Abort the grid when its pins cannot resolve"
    assert green_commit_subject(summary) == f"feat: {summary}"


def test_should_keep_subject_of_exactly_72_chars_untruncated():
    summary = "x" * (72 - len("feat: "))
    assert green_commit_subject(summary) == f"feat: {summary}"


def test_should_cap_long_subject_at_72_chars_without_trailing_space_or_partial_word():
    summary = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike"
    result = green_commit_subject(summary)
    assert len(result) <= 72
    assert result == result.rstrip()
    assert result.startswith("feat: alpha")
    assert result.split()[-1] in summary.split()


def test_should_cut_at_word_boundary_when_summary_is_longer_than_limit():
    summary = ("word " * 30).strip()
    assert green_commit_subject(summary) == ("feat: " + "word " * 13).rstrip()


def test_should_expose_story_derived_subject_when_env_var_set_at_import():
    mod = load_oracle_module_with_env({"LOCAL_AGENT_STORY_SUMMARY": "Fix the widget"})
    assert mod.GREEN_COMMIT_SUBJECT == "feat: Fix the widget"


def test_should_expose_placeholder_subject_when_env_var_unset():
    mod = load_oracle_module_with_env({"LOCAL_AGENT_STORY_SUMMARY": None})
    assert mod.GREEN_COMMIT_SUBJECT == PLACEHOLDER


def _drive_finish_if_green(monkeypatch, mod):
    commits = []
    monkeypatch.setattr(mod, "oracle_result", lambda: (True, "(stub)"))
    monkeypatch.setattr(mod, "worktree_dirty", lambda: True)
    monkeypatch.setattr(mod, "write_done_marker", lambda rc: None)
    monkeypatch.setattr(mod, "auto_commit", commits.append)
    assert mod.finish_if_green(1, messages=[]) is True
    return commits


def test_should_commit_with_story_derived_subject_from_finish_if_green(monkeypatch):
    mod = load_oracle_module_with_env({
        "LOCAL_AGENT_STORY_SUMMARY": "Fix the widget",
        "LOCAL_AGENT_REWORK_FULL_SUITE": None,
        "LOCAL_AGENT_FULL_SUITE_DONE_BAR": None,
        "LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": None,
    })
    assert _drive_finish_if_green(monkeypatch, mod) == ["feat: Fix the widget"]


def test_should_commit_with_placeholder_from_finish_if_green_when_no_summary(monkeypatch):
    mod = load_oracle_module_with_env({
        "LOCAL_AGENT_STORY_SUMMARY": None,
        "LOCAL_AGENT_REWORK_FULL_SUITE": None,
        "LOCAL_AGENT_FULL_SUITE_DONE_BAR": None,
        "LOCAL_AGENT_REVIEW_FEEDBACK_REWORK": None,
    })
    assert _drive_finish_if_green(monkeypatch, mod) == [PLACEHOLDER]
