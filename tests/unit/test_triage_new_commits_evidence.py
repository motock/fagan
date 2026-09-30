"""Unit tests for the tri-state new-commits evidence in ``pipeline.triage``.

``_current_git_state`` renders the ``GIT STATE:`` evidence the overlord rules
on. Its new-commits line used to be driven by a bool helper that answers False
both for "the branch really has no commits beyond base" and for "the base does
not exist in this worktree" - so triage asserted a false fact about the branch
(live 2026-09-29: plan ``language-agnostic-gates`` story LAG-2 was parked on
"no new commits" while carrying five).

The fix routes the render through the tri-state helper
``pipeline.git_ops._worktree_new_commits`` (``"yes"``/``"no"``/``"unknown"``,
plus the base it actually compared) and resolves the changed-files base in the
worktree's own repo via ``pipeline.git_ops._resolve_base_branch``.

These tests are written FIRST (TDD): they exercise the tri-state seam, which
``pipeline/triage.py`` does not yet use, so they fail until it does. A real
subprocess must NEVER run: every test monkeypatches
``pipeline.triage.subprocess.run`` plus the git helpers.
"""
from pathlib import Path

import pytest

from pipeline import server as pipeline_server
from pipeline import triage

SHA = "abc1234def5678901234567890abcdef12345678"
STORY_KEY = "S1"
# Deliberately distinct names: the hint is what _default_branch() answers, the
# resolved base is what the worktree's own repo yields, and the changed-files
# base is a third value, so a test can tell which one got rendered.
HINT = "trunk"
RESOLVED = "master"
CHANGED_BASE = "release"
WORKTREE = "/some/worktree"


def _never_run(*args, **kwargs):
    pytest.fail("subprocess.run must NOT be called for this case")


def _make_run(stdout=SHA + "\n"):
    """A subprocess.run stub returning a fake CompletedProcess."""
    def _run(*args, **kwargs):
        class _R:
            pass
        r = _R()
        r.returncode = 0
        r.stdout = stdout
        r.stderr = ""
        return r
    return _run


def _story(**extra):
    story = {"key": STORY_KEY, "story_key": STORY_KEY}
    story.update(extra)
    return story


def _patch(monkeypatch, *, state, resolved=RESOLVED, hint=HINT,
           changed_base=CHANGED_BASE, run=None):
    """Patch every seam ``_current_git_state`` may use; return a capture dict."""
    captured = {}
    monkeypatch.setattr(triage, "subprocess.run", run or _make_run())

    def _new_commits(worktree, story_key, base_branch):
        captured["worktree"] = worktree
        captured["story_key"] = story_key
        captured["base_branch"] = base_branch
        return state, resolved

    monkeypatch.setattr(triage, "_worktree_new_commits", _new_commits, raising=False)

    def _bool_helper(*a, **k):
        pytest.fail("_worktree_has_new_commits must NOT be called any more")

    monkeypatch.setattr(triage, "_worktree_has_new_commits", _bool_helper, raising=False)

    def _default_branch():
        captured["default_branch_called"] = True
        return hint

    monkeypatch.setattr(triage, "_default_branch", _default_branch, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", _default_branch)

    def _resolve(worktree, base_branch):
        captured["resolve_worktree"] = worktree
        captured["resolve_hint"] = base_branch
        return changed_base

    monkeypatch.setattr(triage, "_resolve_base_branch", _resolve, raising=False)
    return captured


# ---------------------------------------------------------------------------
# Module-level import contract
# ---------------------------------------------------------------------------

def test_module_imports_the_tri_state_helpers():
    """Both new helpers must be module-level names on ``pipeline.triage``."""
    assert hasattr(triage, "_worktree_new_commits")
    assert hasattr(triage, "_resolve_base_branch")


def test_bool_helper_stays_importable():
    """``pipeline.triage_story_actions`` resolves the bool helper through
    ``_ModuleRef("pipeline.triage", "_worktree_has_new_commits")``, so the
    module must keep exposing it."""
    assert hasattr(triage, "_worktree_has_new_commits")


# ---------------------------------------------------------------------------
# state == "unknown": the false fact must be unsayable
# ---------------------------------------------------------------------------

def test_unknown_state_is_inconclusive_and_never_says_no_new_commits(monkeypatch):
    _patch(monkeypatch, state="unknown", resolved="")
    result = triage._current_git_state(WORKTREE, _story())
    assert "INCONCLUSIVE" in result
    assert "0 new commits" not in result
    assert "NO NEW COMMITS" not in result
    # The reader must still learn WHICH base could not be resolved.
    assert HINT in result
    # ...and the substring reader must not turn it into the false fact.
    assert triage._evidence_shows_no_new_commits(result) is False


# ---------------------------------------------------------------------------
# state == "no": existing readers keep working
# ---------------------------------------------------------------------------

def test_no_state_keeps_the_zero_new_commits_phrasing(monkeypatch):
    _patch(monkeypatch, state="no", resolved=RESOLVED)
    result = triage._current_git_state(WORKTREE, _story())
    assert "0 new commits beyond base" in result
    assert f"BRANCH HAS NO NEW COMMITS vs {RESOLVED}" in result
    assert triage._evidence_shows_no_new_commits(result) is True


# ---------------------------------------------------------------------------
# state == "yes": the resolved base is named, not the caller's hint
# ---------------------------------------------------------------------------

def test_yes_state_names_the_resolved_base(monkeypatch):
    _patch(monkeypatch, state="yes", resolved=RESOLVED, hint=HINT)
    result = triage._current_git_state(WORKTREE, _story())
    assert f"BRANCH HAS NEW COMMITS vs {RESOLVED}: yes" in result
    assert f"BRANCH HAS NEW COMMITS vs {HINT}" not in result
    assert triage._evidence_shows_no_new_commits(result) is False


# ---------------------------------------------------------------------------
# the helper is called with (Path(worktree), story_key, _default_branch()'s answer)
# ---------------------------------------------------------------------------

def test_helper_called_with_path_story_key_and_hint(monkeypatch):
    captured = _patch(monkeypatch, state="yes", resolved=RESOLVED, hint=HINT)
    triage._current_git_state(WORKTREE, _story())
    assert isinstance(captured["worktree"], Path)
    assert str(captured["worktree"]) == WORKTREE
    assert captured["story_key"] == STORY_KEY
    assert captured["base_branch"] == HINT
    assert captured["default_branch_called"] is True


# ---------------------------------------------------------------------------
# changed-files section: the base it actually diffs
# ---------------------------------------------------------------------------

def test_changed_files_header_names_the_base_resolved_in_the_worktree(monkeypatch):
    captured = _patch(monkeypatch, state="yes", hint=HINT, changed_base=CHANGED_BASE)
    result = triage._current_git_state(WORKTREE, _story())
    assert f"CHANGED FILES vs {CHANGED_BASE}:" in result
    assert f"CHANGED FILES vs {HINT}:" not in result
    assert isinstance(captured["resolve_worktree"], Path)
    assert str(captured["resolve_worktree"]) == WORKTREE
    assert captured["resolve_hint"] == HINT


def test_changed_files_header_falls_back_to_the_hint_when_nothing_resolves(monkeypatch):
    _patch(monkeypatch, state="yes", hint=HINT, changed_base="")
    result = triage._current_git_state(WORKTREE, _story())
    assert f"CHANGED FILES vs {HINT}:" in result
    assert "(unavailable)" in result


# ---------------------------------------------------------------------------
# _evidence_shows_no_new_commits: the reader of that substring
# ---------------------------------------------------------------------------

def test_evidence_reader_true_for_the_no_new_commits_fact():
    assert triage._evidence_shows_no_new_commits(
        "BRANCH HAS NO NEW COMMITS vs master (0 new commits beyond base)"
    ) is True


def test_evidence_reader_false_for_inconclusive_section():
    assert triage._evidence_shows_no_new_commits(
        "GIT STATE: HEAD abc1234\n"
        "BASE BRANCH trunk UNRESOLVED in this worktree; "
        "new-commits check INCONCLUSIVE"
    ) is False


def test_inconclusive_section_wins_over_a_stale_no_new_commits_string():
    """An INCONCLUSIVE section must never be read as the fact, even when the
    same evidence carries the old phrasing elsewhere (a stale parked_reason)."""
    assert triage._evidence_shows_no_new_commits(
        "GIT STATE: HEAD abc1234\n"
        "BASE BRANCH trunk UNRESOLVED in this worktree; "
        "new-commits check INCONCLUSIVE\n"
        "PARKED REASON: no new commits vs master (0 new commits beyond base)"
    ) is False


@pytest.mark.parametrize(
    "evidence",
    [
        "",
        "GIT STATE: HEAD abc1234",
        "CHANGED FILES vs master:\n(unavailable)",
        "BRANCH HAS NEW COMMITS vs master: yes",
    ],
)
def test_evidence_reader_false_for_unrelated_evidence(evidence):
    assert triage._evidence_shows_no_new_commits(evidence) is False


def test_evidence_reader_requires_its_argument():
    with pytest.raises(TypeError):
        triage._evidence_shows_no_new_commits()
