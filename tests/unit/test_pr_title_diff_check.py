"""Warn-only heuristic: a PR title sharing no words with the story summary or
the changed files triggers one ``pr_title_mismatch`` notification.

Only ``subprocess.run`` (git / gh) is faked, at the external boundary.
"""

import subprocess

from pipeline import pr as pr_mod
from pipeline import review_orchestrator as ro

WORKTREE = "wt"
BRANCH = "agent/ptd-1"
CASE_1119_TITLE = "docs: revert out-of-scope CLAUDE.md change"
CASE_1119_SUMMARY = "Document the Python 3.12 floor in the README"


def _proc(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _fake_run(responses):
    """responses: list of (argv-prefix, result-or-exception) matched in order."""

    def run(argv, **_kwargs):
        for prefix, result in responses:
            if argv[: len(prefix)] == prefix:
                if isinstance(result, Exception):
                    raise result
                return result
        return _proc(returncode=1)

    return run


# --- _title_describes_change -------------------------------------------------


def test_should_accept_title_sharing_a_summary_word():
    assert pr_mod._title_describes_change("feat: add parser", "Parser for configs", []) is True


def test_should_accept_title_sharing_a_changed_path_word():
    assert pr_mod._title_describes_change("docs: update readme", "Something else", ["README.md"]) is True


def test_should_reject_pr_1119_case():
    assert pr_mod._title_describes_change(CASE_1119_TITLE, CASE_1119_SUMMARY, ["README.md"]) is False


def test_should_fail_open_on_empty_title():
    assert pr_mod._title_describes_change("", "Anything here", ["a.py"]) is True


def test_should_fail_open_when_title_is_only_type_and_stopwords():
    assert pr_mod._title_describes_change("fix: with this", "Unrelated summary", ["x.py"]) is True


def test_should_fail_open_on_empty_summary_and_paths():
    assert pr_mod._title_describes_change("feat: widget factory", "", []) is True


def test_should_not_count_three_letter_overlap():
    assert pr_mod._title_describes_change("feat: widget factory", "Fix the cat bug in logging", ["env.py"]) is False


# --- _changed_paths ----------------------------------------------------------


def test_changed_paths_returns_files_on_success(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _fake_run([
        (["git", "merge-base"], _proc("abc\n")),
        (["git", "diff"], _proc("README.md\npipeline/pr.py\n")),
    ]))
    assert pr_mod._changed_paths(WORKTREE, BRANCH) == ["README.md", "pipeline/pr.py"]


def test_changed_paths_empty_when_git_diff_fails(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _fake_run([
        (["git", "merge-base"], _proc("abc\n")),
        (["git", "diff"], _proc(returncode=128)),
    ]))
    assert pr_mod._changed_paths(WORKTREE, BRANCH) == []


def test_changed_paths_empty_when_no_merge_base(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _fake_run([
        (["git", "merge-base"], _proc(returncode=1)),
    ]))
    assert pr_mod._changed_paths(WORKTREE, BRANCH) == []


def test_changed_paths_empty_on_oserror(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _fake_run([
        (["git", "merge-base"], OSError("no git")),
    ]))
    assert pr_mod._changed_paths(WORKTREE, BRANCH) == []


# --- _pr_title_mismatch_message ----------------------------------------------


def _gh_and_git(title_result):
    return _fake_run([
        (["gh", "pr", "view"], title_result),
        (["git", "merge-base"], _proc("abc\n")),
        (["git", "diff"], _proc("README.md\n")),
    ])


def test_message_names_title_and_summary_for_1119_case(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _gh_and_git(_proc(CASE_1119_TITLE + "\n")))
    msg = pr_mod._pr_title_mismatch_message(WORKTREE, BRANCH, CASE_1119_SUMMARY)
    assert msg is not None
    assert CASE_1119_TITLE in msg
    assert CASE_1119_SUMMARY in msg


def test_message_none_for_matching_title(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _gh_and_git(_proc("docs: document the README floor\n")))
    assert pr_mod._pr_title_mismatch_message(WORKTREE, BRANCH, CASE_1119_SUMMARY) is None


def test_message_none_when_gh_fails(monkeypatch):
    monkeypatch.setattr(pr_mod.subprocess, "run", _gh_and_git(_proc(returncode=1)))
    assert pr_mod._pr_title_mismatch_message(WORKTREE, BRANCH, CASE_1119_SUMMARY) is None


def test_message_none_when_collaborator_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pr_mod, "_changed_paths", boom)
    monkeypatch.setattr(pr_mod.subprocess, "run", _gh_and_git(_proc(CASE_1119_TITLE + "\n")))
    assert pr_mod._pr_title_mismatch_message(WORKTREE, BRANCH, CASE_1119_SUMMARY) is None


# --- wiring in review_orchestrator -------------------------------------------


def _wire(monkeypatch, message):
    sent = []
    monkeypatch.setattr(ro, "_notify_user", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(ro, "_resolve_story_branch", lambda _wt, _key: BRANCH)
    if isinstance(message, Exception):
        def raiser(*_a, **_k):
            raise message
        monkeypatch.setattr(ro, "_pr_title_mismatch_message", raiser)
    else:
        monkeypatch.setattr(ro, "_pr_title_mismatch_message", lambda *_a: message)
    return sent


def test_wiring_emits_one_notification_on_message(monkeypatch):
    sent = _wire(monkeypatch, "mismatch")
    ro._warn_on_pr_title_mismatch("plan", "PTD-1", {"summary": "s"}, WORKTREE)
    assert len(sent) == 1
    assert sent[0][1]["event"] == "pr_title_mismatch"
    assert sent[0][1]["severity"] == "warning"
    assert sent[0][1]["story_key"] == "PTD-1"


def test_wiring_emits_nothing_when_no_message(monkeypatch):
    sent = _wire(monkeypatch, None)
    ro._warn_on_pr_title_mismatch("plan", "PTD-1", {"summary": "s"}, WORKTREE)
    assert sent == []


def test_wiring_swallows_exception_from_check(monkeypatch):
    sent = _wire(monkeypatch, RuntimeError("boom"))
    ro._warn_on_pr_title_mismatch("plan", "PTD-1", {"summary": "s"}, WORKTREE)
    assert sent == []
