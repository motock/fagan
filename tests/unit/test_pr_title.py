"""The PR title must not repeat a story key the summary already opens with.

Plan authors routinely title a summary with its own key (``SU-2: <what it
does>``), and ``_open_pr`` blindly prefixed the key again, producing
``SU-2: SU-2: ...``. That title is not cosmetic: ``_merge_pr`` recovers it via
``gh pr view --json title`` and passes it as ``--subject``, so the doubled
title becomes the permanent subject of the squash-merge commit on the default
branch.

Written FIRST (TDD): every test below is RED against the current
implementation (``AttributeError`` on ``pipeline.pr._pr_title``), reached by
attribute access so collection still succeeds before the helper exists.
"""

import subprocess
from pathlib import Path

from pipeline import pr

# pipeline.story_status imports pipeline.server at module level and
# pipeline.server imports check_story_status back from story_status, so
# pipeline.server must be imported FIRST or collection dies on the cycle.
from pipeline import server as p


def test_should_not_repeat_a_key_the_summary_already_opens_with():
    assert (
        pr._pr_title("SU-2", "SU-2: Stop doubling the PR title")
        == "SU-2: Stop doubling the PR title"
    )


def test_should_prefix_a_summary_that_does_not_carry_the_key():
    assert pr._pr_title("SU-2", "Stop doubling the PR title") == (
        "SU-2: Stop doubling the PR title"
    )


def test_should_prefix_a_summary_that_opens_with_another_key():
    assert pr._pr_title("SU-2", "SU-9: Stop doubling the PR title") == (
        "SU-2: SU-9: Stop doubling the PR title"
    )


def test_should_not_confuse_a_longer_key_that_merely_starts_the_same_way():
    """``SU-20:`` is a different key, not this story's key plus a suffix."""
    assert pr._pr_title("SU-2", "SU-20: Stop doubling the PR title") == (
        "SU-2: SU-20: Stop doubling the PR title"
    )


def test_should_match_the_key_case_insensitively():
    """The branch and manifest key may be lower-cased while the summary keeps
    the author's spelling; either way the title must not double the key."""
    assert pr._pr_title("su-2", "SU-2: Stop doubling") == "SU-2: Stop doubling"
    assert pr._pr_title("SU-2", "su-2: Stop doubling") == "su-2: Stop doubling"


def test_should_handle_a_missing_summary():
    """A missing summary used to yield the literal 'SU-2: None'."""
    assert pr._pr_title("SU-2", None) == "SU-2: "
    assert pr._pr_title("SU-2", "") == "SU-2: "


def test_open_pr_passes_the_non_repeating_title_to_gh(monkeypatch, tmp_path):
    """The wiring that matters: ``gh pr create`` is what records the title."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    seen = []

    def run_mock(cmd, **kwargs):
        seen.append(list(cmd))
        if list(cmd)[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="agent/su-2\n", stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://github.com/x/y/pull/7\n", stderr=""
        )

    monkeypatch.setattr(p.subprocess, "run", run_mock)

    url = p._open_pr(str(worktree), "SU-2", {"summary": "SU-2: Stop doubling"})

    assert url == "https://github.com/x/y/pull/7"
    create = [c for c in seen if c[:3] == ["gh", "pr", "create"]]
    assert len(create) == 1, seen
    assert create[0][create[0].index("--title") + 1] == "SU-2: Stop doubling"


def test_pr_module_has_no_second_copy_of_the_title_logic():
    assert "def _pr_title(" in Path(pr.__file__).read_text()
    assert 'title = f"{story_key}: {story[\'summary\']}"' not in Path(pr.__file__).read_text()
