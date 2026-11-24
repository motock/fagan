"""Pure helpers for the rebase-conflict rework brief (RC-01).

``parse_conflict_files`` extracts the conflicted paths git prints, and
``conflict_rework_feedback`` renders the brief handed to the implementer when
the merge gate's rebase conflicts. ``parse_unresolved`` makes the
``REBASE-CONFLICT-UNRESOLVED:`` marker machine-readable for a later story.
"""
import pytest

from pipeline import advance_merge
from pipeline.rebase_conflict_feedback import (
    CONFLICT_REWORK_MAX_DEFAULT,
    _conflict_rework_max,
    _merge_ci_rework_feedback,
    conflict_rework_feedback,
    parse_conflict_files,
    parse_unresolved,
    route_resumed_conflict,
)

MULTI_FILE_ERROR = """Auto-merging src/app.py
CONFLICT (content): Merge conflict in src/app.py
Auto-merging src/util.py
CONFLICT (content): Merge conflict in src/util.py
CONFLICT (content): Merge conflict in src/app.py
CONFLICT (add/add): Merge conflict in docs/new guide.md
CONFLICT (content): Merge conflict in ../outside.py
CONFLICT (content): Merge conflict in /abs/path.py
CONFLICT (modify/delete): legacy/old.py deleted in HEAD and modified in 1234abc.  Version 1234abc of legacy/old.py left in tree.
"""


def test_parse_conflict_files_multi_file():
    assert parse_conflict_files(MULTI_FILE_ERROR) == [
        "src/app.py",
        "src/util.py",
        "docs/new guide.md",
    ]


def test_parse_conflict_files_dedupes():
    error = (
        "CONFLICT (content): Merge conflict in a.py\n"
        "CONFLICT (content): Merge conflict in a.py\n"
        "CONFLICT (content): Merge conflict in b.py\n"
    )
    assert parse_conflict_files(error) == ["a.py", "b.py"]


def test_parse_conflict_files_drops_bad_paths():
    error = (
        "CONFLICT (content): Merge conflict in ../x.py\n"
        "CONFLICT (content): Merge conflict in /etc/x.py\n"
        "CONFLICT (content): Merge conflict in src/a..b.py\n"
        "CONFLICT (content): Merge conflict in src/bad\x07name.py\n"
    )
    assert parse_conflict_files(error) == []


def test_parse_conflict_files_empty_and_garbage():
    assert parse_conflict_files("") == []
    assert parse_conflict_files("fatal: not a git repository") == []
    assert parse_conflict_files("CONFLICT (content): Merge conflict in ") == []


def test_feedback_mentions_files_branch_attempt_and_rules():
    fb = conflict_rework_feedback(
        MULTI_FILE_ERROR, attempt=2, max_attempts=5, default_branch="main"
    )
    assert "attempt 2/5" in fb
    assert "origin/main" in fb
    assert "git fetch origin" in fb
    assert "git rebase origin/main" in fb
    assert "git log origin/main -3" in fb
    assert "git add" in fb
    assert "git rebase --continue" in fb
    assert "git push" in fb
    assert "REBASE-CONFLICT-UNRESOLVED:" in fb
    assert "src/app.py" in fb
    assert "src/util.py" in fb
    assert "docs/new guide.md" in fb


def test_feedback_no_files_phrase():
    fb = conflict_rework_feedback("", 1, 3, "main")
    assert "run the rebase and resolve whatever git reports" in fb
    assert "attempt 1/3" in fb
    assert "git push" in fb
    assert "REBASE-CONFLICT-UNRESOLVED:" in fb


def test_feedback_length_cap_200_files():
    error = "\n".join(
        f"CONFLICT (content): Merge conflict in src/module_{i:03d}.py"
        for i in range(200)
    )
    fb = conflict_rework_feedback(error, 1, 3, "main")
    assert len(fb) <= 2000
    assert "attempt 1/3" in fb
    assert "git push" in fb
    assert "REBASE-CONFLICT-UNRESOLVED:" in fb


def test_parse_unresolved_last_marker():
    summary = (
        "did stuff\n"
        "REBASE-CONFLICT-UNRESOLVED: src/a.py :: both sides rewrote parse()\n"
        "more chatter\n"
        "REBASE-CONFLICT-UNRESOLVED: src/b.py :: migration 0042 conflicts\n"
        "done\n"
    )
    assert parse_unresolved(summary) == "src/b.py :: migration 0042 conflicts"


def test_parse_unresolved_none():
    assert parse_unresolved("") is None
    assert parse_unresolved("all good") is None
    assert parse_unresolved("Note: REBASE-CONFLICT-UNRESOLVED: x") is None


def test_parse_unresolved_caps_and_strips_control():
    long_reason = "REBASE-CONFLICT-UNRESOLVED: " + "x" * 350
    assert len(parse_unresolved(long_reason)) == 300

    result = parse_unresolved("REBASE-CONFLICT-UNRESOLVED: src/a.py :: why \x07")
    assert result == "src/a.py :: why"
    assert "\x07" not in result
    assert not result.endswith(" ")


from pipeline.rebase_conflict_feedback import conflict_handoff_pending


def test_handoff_pending_is_true_when_the_marker_is_true():
    assert conflict_handoff_pending({"conflict_handoff": True}) is True


def test_handoff_pending_is_false_when_the_marker_is_missing():
    assert conflict_handoff_pending({"status": "changes_requested"}) is False


def test_handoff_pending_is_false_when_the_marker_is_false():
    assert conflict_handoff_pending({"conflict_handoff": False}) is False


def test_handoff_pending_rejects_a_truthy_non_bool_marker():
    assert conflict_handoff_pending({"conflict_handoff": "yes"}) is False
    assert conflict_handoff_pending({"conflict_handoff": 1}) is False


# ---------- RC-02: budget helpers moved here from pipeline.advance_merge ----------


def test_advance_merge_still_exposes_conflict_rework_max():
    assert advance_merge._conflict_rework_max is _conflict_rework_max
    assert advance_merge.CONFLICT_REWORK_MAX_DEFAULT == CONFLICT_REWORK_MAX_DEFAULT


def test_conflict_rework_max_defaults_to_two_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_CONFLICT_REWORK_MAX", raising=False)
    assert _conflict_rework_max() == 2


def test_conflict_rework_max_reads_an_explicit_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "5")
    assert _conflict_rework_max() == 5


def test_conflict_rework_max_zero_is_a_valid_never_route(monkeypatch):
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "0")
    assert _conflict_rework_max() == 0


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5", ""])
def test_conflict_rework_max_falls_back_to_default_on_bad_input(monkeypatch, raw):
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", raw)
    assert _conflict_rework_max() == CONFLICT_REWORK_MAX_DEFAULT


def test_merge_ci_rework_feedback_is_the_same_function_in_both_modules():
    assert advance_merge._merge_ci_rework_feedback is _merge_ci_rework_feedback


def test_merge_ci_rework_feedback_returns_ci_text_when_nothing_is_prior():
    assert _merge_ci_rework_feedback(None, "CI broke") == "CI broke"


# ---------- RC-02: route_resumed_conflict ----------

_ERROR = "CONFLICT (content): Merge conflict in src/app.py"


def test_route_resumed_conflict_counts_and_briefs_when_budget_remains(monkeypatch):
    monkeypatch.delenv("PIPELINE_CONFLICT_REWORK_MAX", raising=False)
    monkeypatch.delenv("PIPELINE_REWORK_ON_CONFLICT", raising=False)
    story = {"conflict_reworks": 0, "review_feedback": "Keep the cache."}

    assert route_resumed_conflict(story, _ERROR, "master") is True

    assert story["conflict_reworks"] == 1
    assert story["ci_rework"] is True
    assert "conflict_handoff" not in story
    assert "REBASE CONFLICT - attempt 1/2" in story["review_feedback"]
    assert "src/app.py" in story["review_feedback"]
    assert story["review_feedback"].startswith("Earlier review feedback")
    assert "Keep the cache." in story["review_feedback"]


def test_route_resumed_conflict_leaves_the_story_alone_when_spent(monkeypatch):
    monkeypatch.delenv("PIPELINE_CONFLICT_REWORK_MAX", raising=False)
    story = {"conflict_reworks": 2}

    assert route_resumed_conflict(story, _ERROR, "master") is False
    assert story == {"conflict_reworks": 2}


def test_route_resumed_conflict_leaves_the_story_alone_when_disabled(monkeypatch):
    monkeypatch.setenv("PIPELINE_REWORK_ON_CONFLICT", "0")
    story = {}

    assert route_resumed_conflict(story, _ERROR, "master") is False
    assert story == {}


def test_route_resumed_conflict_does_not_count_a_pending_handoff(monkeypatch):
    monkeypatch.setenv("PIPELINE_CONFLICT_REWORK_MAX", "0")
    story = {"conflict_handoff": True, "conflict_reworks": 1}

    assert route_resumed_conflict(story, _ERROR, "master") is True
    assert story == {"conflict_handoff": True, "conflict_reworks": 1}
