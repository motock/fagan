"""Unit tests for ``pipeline.triage._current_git_state``.

``_current_git_state`` is a LIVE git-state probe that mirrors
``_current_suite_state``'s fail-open shape: it never raises, and it returns
``""`` (silence) on any problem. It exists because 21 of 34 historical parked
stories were parked on a now-stale "no new commits vs master" reason that live
git state contradicted - the overlord must never act on ``parked_reason`` text.

It reports three facts as a ``GIT STATE:`` section:

  (1) the worktree's current HEAD sha;
  (2) whether the story's branch has NEW COMMITS vs the base branch, reusing
      ``pipeline.git_ops._worktree_has_new_commits`` and resolving the base
      branch the way its existing callers do (``_default_branch()``);
  (3) the story's ``pr_url`` when present.

These tests are written FIRST (TDD). They import ``pipeline.triage``, which does
not yet expose ``_current_git_state`` (nor the module-level
``_worktree_has_new_commits`` import), so they currently fail with
``AttributeError`` - the correct RED state. A later dispatch implements
``pipeline/triage.py`` against them.

CRITICAL: a real subprocess must NEVER run from inside this test module. Every
test monkeypatches ``pipeline.triage.subprocess.run`` - the module's
``globals()["subprocess.run"]`` seam - plus the git helpers before exercising
the function.
"""
import re

import pytest

from pipeline import server as pipeline_server
from pipeline import triage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SHA = "abc1234def5678901234567890abcdef12345678"
PR_URL = "https://example.test/pr/42"
STORY_KEY = "S1"
# Deliberately NOT "main"/"master": a distinctive value makes it obvious when
# the implementation guesses a base branch instead of resolving it.
BASE = "trunk"


def _never_run(*args, **kwargs):
    """A stub for subprocess.run that fails the test if ever invoked."""
    pytest.fail("subprocess.run must NOT be called for this case")


def _make_run(returncode=0, stdout="", stderr=""):
    """Build a subprocess.run stub returning a fake CompletedProcess."""
    def _run(*args, **kwargs):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r
    return _run


def _patch_has_new(monkeypatch, fn):
    """Patch ``_worktree_has_new_commits`` in every namespace it may be read from.

    The implementation may bind it at module level
    (``triage._worktree_has_new_commits``) or import it lazily from
    ``pipeline.git_ops``; patch both so the behavioural tests grade behaviour
    rather than import style. ``git_ops`` is imported lazily here to avoid a
    circular import at test-module load time.
    """
    from pipeline import git_ops

    monkeypatch.setattr(triage, "_worktree_has_new_commits", fn, raising=False)
    monkeypatch.setattr(git_ops, "_worktree_has_new_commits", fn)


def _story(**extra):
    """A story dict carrying its key under both plausible field names.

    The manifest keys stories by story key, so the story dict itself may carry
    the key as either ``key`` (the convention in ``pipeline.wedge_io`` /
    ``pipeline.ingest``) or ``story_key``. Setting both keeps these tests
    agnostic about which one the implementation reads while still grading that
    the key is threaded through to ``_worktree_has_new_commits``.
    """
    story = {"key": STORY_KEY, "story_key": STORY_KEY}
    story.update(extra)
    return story


def _patch_git(monkeypatch, *, sha=SHA, has_new=True, base=BASE, run=None):
    """Patch every seam ``_current_git_state`` may use; return a capture dict."""
    captured = {}
    if run is None:
        run = _make_run(returncode=0, stdout=sha + "\n")
    monkeypatch.setattr(triage, "subprocess.run", run)

    def _has_new(worktree, story_key, base_branch):
        captured["worktree"] = worktree
        captured["story_key"] = story_key
        captured["base_branch"] = base_branch
        return has_new

    # Patch both the triage-namespace binding (module-level import) and the
    # git_ops-namespace one (lazy `from .git_ops import ...`), so the test works
    # whichever way the implementation reuses the helper.
    _patch_has_new(monkeypatch, _has_new)

    def _default_branch():
        captured["default_branch_called"] = True
        return base

    # Patch both the triage-namespace binding (module-level import) and the
    # server-namespace one (lazy `from .server import _default_branch`), so the
    # test works whichever way the implementation resolves the base branch.
    monkeypatch.setattr(triage, "_default_branch", _default_branch, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", _default_branch)
    return captured


# ---------------------------------------------------------------------------
# Module-level import contract
# ---------------------------------------------------------------------------

def test_module_imports_worktree_has_new_commits():
    """The module must import _worktree_has_new_commits at module level so it is
    patchable as triage._worktree_has_new_commits (codebase convention)."""
    assert hasattr(triage, "_worktree_has_new_commits")


def test_current_git_state_in_all():
    """_current_git_state must be exported in __all__ (codebase convention for
    underscore-prefixed helpers that tests reference directly)."""
    assert hasattr(triage, "__all__")
    assert "_current_git_state" in triage.__all__


def test_current_git_state_exists():
    assert hasattr(triage, "_current_git_state")
    assert callable(triage._current_git_state)


def test_current_git_state_docstring_mentions_fail_open():
    """The docstring must state the fail-open / never-raises contract."""
    doc = triage._current_git_state.__doc__ or ""
    assert doc, "_current_git_state must have a docstring"
    low = doc.lower()
    assert "fail" in low or "never raise" in low or "silence" in low


# ---------------------------------------------------------------------------
# Empty / falsy / non-string worktree guard
# ---------------------------------------------------------------------------

def test_empty_worktree_returns_empty_and_never_runs(monkeypatch):
    """An empty worktree must return '' immediately and never probe git."""
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    _patch_has_new(
        monkeypatch,
        lambda *a, **k: pytest.fail("_worktree_has_new_commits must NOT be called"),
    )
    assert triage._current_git_state("", _story()) == ""


def test_none_worktree_returns_empty_and_never_runs(monkeypatch):
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    _patch_has_new(
        monkeypatch,
        lambda *a, **k: pytest.fail("_worktree_has_new_commits must NOT be called"),
    )
    assert triage._current_git_state(None, _story()) == ""


@pytest.mark.parametrize("bad", [123, 0, [], {}, 3.14])
def test_non_string_worktree_returns_empty(monkeypatch, bad):
    """A non-string worktree must fail open to '' rather than raising."""
    def _boom(*a, **k):
        raise TypeError("cwd must be str or PathLike")

    monkeypatch.setattr(triage, "subprocess.run", _boom)
    _patch_has_new(monkeypatch, _boom)
    monkeypatch.setattr(triage, "_default_branch", lambda: BASE, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", lambda: BASE)
    assert triage._current_git_state(bad, _story()) == ""


# ---------------------------------------------------------------------------
# Fact (1): current HEAD sha
# ---------------------------------------------------------------------------

def test_head_sha_appears_in_section(monkeypatch):
    _patch_git(monkeypatch, sha="deadbeefcafe1234", has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", _story())
    assert isinstance(result, str)
    assert "GIT STATE:" in result
    assert "deadbeefcafe1234" in result


# ---------------------------------------------------------------------------
# Fact (2): new commits vs the base branch
# ---------------------------------------------------------------------------

def test_new_commits_vs_base_names_the_fact(monkeypatch):
    captured = _patch_git(monkeypatch, has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", _story())
    assert "GIT STATE:" in result
    assert SHA in result
    low = result.lower()
    assert "new commit" in low
    # The True case must NOT read like the False case.
    assert "no new commit" not in low
    assert BASE in result
    assert captured["base_branch"] == BASE


def test_no_new_commits_names_that(monkeypatch):
    captured = _patch_git(monkeypatch, has_new=False, base=BASE)
    result = triage._current_git_state("/some/worktree", _story())
    assert "GIT STATE:" in result
    low = result.lower()
    # Any natural phrasing of "the branch has no commits beyond base".
    assert any(
        tok in low
        for tok in (
            "no new commit",
            "0 new commit",
            "without new commit",
            "no commits",
        )
    )
    assert BASE in result
    assert captured["base_branch"] == BASE


def test_worktree_has_new_commits_called_with_path_and_story_key(monkeypatch):
    """The reused helper takes (worktree: Path, story_key, base_branch)."""
    captured = _patch_git(monkeypatch, has_new=True, base=BASE)
    triage._current_git_state("/some/worktree", _story())
    assert isinstance(captured["worktree"], triage.Path)
    assert str(captured["worktree"]) == "/some/worktree"
    assert captured["story_key"] == STORY_KEY
    assert captured["base_branch"] == BASE


def test_base_branch_resolved_via_default_branch(monkeypatch):
    """The base branch must come from _default_branch(), the resolution its
    existing callers use - not a hardcoded 'main'/'master' guess."""
    captured = _patch_git(monkeypatch, has_new=True, base="release-9")
    result = triage._current_git_state("/some/worktree", _story())
    assert captured.get("default_branch_called") is True
    assert captured["base_branch"] == "release-9"
    assert "release-9" in result


def test_base_branch_unresolved_reports_fact_and_does_not_guess(monkeypatch):
    """If the base branch cannot be resolved, report that fact rather than
    guessing a branch name."""
    captured = _patch_git(monkeypatch, has_new=True, base="")
    result = triage._current_git_state("/some/worktree", _story())
    assert "GIT STATE:" in result
    low = result.lower()
    assert any(
        tok in low
        for tok in (
            "unresolved",
            "could not",
            "cannot",
            "can't",
            "unknown",
            "unavailable",
            "no base branch",
            "skipped",
            "not resolved",
        )
    )
    # The helper must NOT be handed a guessed branch name: either it is not
    # called at all, or it is called with an empty base branch.
    assert captured.get("base_branch") in (None, "")
    # And no guessed branch name leaks into the rendered section
    # (word-boundary so "domain"/"mainline" don't trip it).
    assert not re.search(r"\bmain\b", low)
    assert not re.search(r"\bmaster\b", low)


# ---------------------------------------------------------------------------
# Fact (3): pr_url
# ---------------------------------------------------------------------------

def test_pr_url_present_appears_in_section(monkeypatch):
    _patch_git(monkeypatch, has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", _story(pr_url=PR_URL))
    assert "GIT STATE:" in result
    assert PR_URL in result


def test_pr_url_absent_is_handled(monkeypatch):
    _patch_git(monkeypatch, has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", _story())
    assert "GIT STATE:" in result
    assert PR_URL not in result


def test_pr_url_empty_string_is_handled(monkeypatch):
    _patch_git(monkeypatch, has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", _story(pr_url=""))
    assert "GIT STATE:" in result
    assert PR_URL not in result


def test_empty_story_dict_does_not_raise(monkeypatch):
    _patch_git(monkeypatch, has_new=True, base=BASE)
    result = triage._current_git_state("/some/worktree", {})
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Fail-open / never-raises
# ---------------------------------------------------------------------------

def test_subprocess_raising_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no git binary")

    _patch_git(monkeypatch, run=_boom)
    assert triage._current_git_state("/some/worktree", _story()) == ""


def test_worktree_has_new_commits_raising_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("git log blew up")

    monkeypatch.setattr(triage, "subprocess.run",
                        _make_run(returncode=0, stdout=SHA + "\n"))
    _patch_has_new(monkeypatch, _boom)
    monkeypatch.setattr(triage, "_default_branch", lambda: BASE, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", lambda: BASE)
    assert triage._current_git_state("/some/worktree", _story()) == ""


def test_default_branch_raising_does_not_raise(monkeypatch):
    def _boom():
        raise RuntimeError("cannot resolve default branch")

    monkeypatch.setattr(triage, "subprocess.run",
                        _make_run(returncode=0, stdout=SHA + "\n"))
    _patch_has_new(monkeypatch, lambda *a, **k: True)
    monkeypatch.setattr(triage, "_default_branch", _boom, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", _boom)
    result = triage._current_git_state("/some/worktree", _story())
    assert isinstance(result, str)


def test_never_raises_on_arbitrary_story(monkeypatch):
    """A story full of junk must still fail open, never raise."""
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    _patch_has_new(
        monkeypatch,
        lambda *a, **k: pytest.fail("_worktree_has_new_commits must NOT be called"),
    )
    assert triage._current_git_state("", {"pr_url": object(), "key": object()}) == ""


# ---------------------------------------------------------------------------
# Wiring into collect_triage_evidence
# ---------------------------------------------------------------------------

def _patch_evidence_seams(monkeypatch, git_state):
    """Stub every seam collect_triage_evidence touches so no real work runs."""
    monkeypatch.setattr(triage, "_current_suite_state", lambda w: "")
    monkeypatch.setattr(triage, "collect_failure_evidence",
                        lambda w, s, limit=6000: "")
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(triage, "_current_git_state", git_state)


def test_collect_triage_evidence_includes_git_state_section(monkeypatch):
    _patch_evidence_seams(monkeypatch, lambda w, s: "GIT STATE: sentinel-xyz")
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}
    )
    assert "GIT STATE: sentinel-xyz" in result


def test_collect_triage_evidence_calls_current_git_state_with_worktree_and_story(
    monkeypatch,
):
    seen = {}

    def _git_state(worktree, story):
        seen["worktree"] = worktree
        seen["story"] = story
        return ""

    _patch_evidence_seams(monkeypatch, _git_state)
    story = {"status": "parked", "key": STORY_KEY}
    triage.collect_triage_evidence("/nonexistent/worktree", story)
    assert seen["worktree"] == "/nonexistent/worktree"
    assert seen["story"] is story


def test_collect_triage_evidence_git_state_raising_does_not_propagate(monkeypatch):
    def _boom(w, s):
        raise RuntimeError("git state blew up")

    _patch_evidence_seams(monkeypatch, _boom)
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}
    )
    assert "TRIAGE QUESTION" in result
    assert "STORY STATE:" in result


def test_collect_triage_evidence_limit_respected_with_git_state(monkeypatch):
    _patch_evidence_seams(monkeypatch, lambda w, s: "GIT STATE: " + "Z" * 100000)
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=8000
    )
    assert len(result) <= 8000
    assert "STORY STATE:" in result


def test_collect_triage_evidence_git_state_is_in_fixed_parts(monkeypatch):
    """GIT STATE must be appended to fixed_parts (like current_state_section),
    i.e. it appears before the failure-evidence tail rather than being dropped
    when the budget is tight."""
    monkeypatch.setattr(triage, "_current_suite_state", lambda w: "")
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(triage, "_current_git_state",
                        lambda w, s: "GIT STATE: fixed-anchor")
    monkeypatch.setattr(triage, "collect_failure_evidence",
                        lambda w, s, limit=6000: "FAILURE EVIDENCE: tail-anchor")
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=8000
    )
    assert "GIT STATE: fixed-anchor" in result
    assert "FAILURE EVIDENCE: tail-anchor" in result
    assert result.index("GIT STATE: fixed-anchor") < result.index(
        "FAILURE EVIDENCE: tail-anchor"
    )
