"""Pure helpers for the rebase-conflict rework brief (RC-01).

``parse_conflict_files`` extracts the conflicted paths git prints, and
``conflict_rework_feedback`` renders the brief handed to the implementer when
the merge gate's rebase conflicts. ``parse_unresolved`` makes the
``REBASE-CONFLICT-UNRESOLVED:`` marker machine-readable for a later story.
"""
from pipeline.rebase_conflict_feedback import (
    conflict_rework_feedback,
    parse_conflict_files,
    parse_unresolved,
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
