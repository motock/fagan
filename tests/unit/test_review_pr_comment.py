"""Tests for the PR-comment posting logic in ``review_story``.

When a story lands on a genuine ``changes_requested`` verdict (a real worktree
exists), ``review_story`` now opens/updates the story's PR and posts the
reviewer's findings as a visible PR comment so a human can see what the
automated gate flagged. This is an UNCONDITIONAL behavior change: the old
"PR must not be opened on REQUEST_CHANGES" contract is gone.

These tests verify that:

* a genuine REQUEST_CHANGES with findings opens the PR once and posts a
  comment whose body contains the raw findings AND the cycle number;
* ``story["pr_url"]`` is set and ``review_feedback`` is stored;
* a ``subprocess.CalledProcessError`` from ``_open_pr`` is swallowed (status
  stays ``changes_requested``, no comment posted, no exception propagates);
* an inconclusive bare REQUEST_CHANGES (no findings) never opens a PR or posts;
* the commit-hygiene autofix SUCCESS path (status ``tests_passed``) never opens
  a PR or posts;
* the rework-cap-exhausted park path never opens a PR or posts;
* the comment body uses the RAW ``reviewer_output`` (no oracle-preservation
  preamble) while ``story["review_feedback"]`` DOES keep that preamble.

The ``os.path.isdir(worktree)`` guard is load-bearing: these tests use a REAL
git worktree (``wt.mkdir()``) so the block actually runs. A fake/non-existent
worktree would skip it and the tests would pass vacuously.
"""

import json
import subprocess
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_final_rework_escalation.py - fully standalone).
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers (replicated from the sibling test files - do not import across).
# ---------------------------------------------------------------------------


def _write_manifest_with_story(plan_dir, plan_name, story_key, story, *,
                               top_level=None):
    manifest = {"epics": {}, "stories": {story_key: story}}
    if top_level:
        manifest.update(top_level)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _make_story(plan_dir, *, status, worktree=None, **extra):
    """Build a minimal story dict in the given state."""
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else str(worktree),
        "risk": "low",
    }
    story.update(extra)
    return story


def _init_git_worktree(tmp_path, *, dirty=False, bad_message="wip(x): checkpoint-1"):
    """Create a real git repo in a tmp_path subdir, commit one file with a
    deliberately bad commit message, and return the repo path. If ``dirty`` is
    True, also leave an uncommitted modification so the tree is not clean."""
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=wt, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=wt, check=True)
    f = wt / "app.py"
    f.write_text("print('hello')\n")
    subprocess.run(["git", "add", "app.py"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", bad_message], cwd=wt, check=True)
    if dirty:
        f.write_text("print('hello world')\n")
    return str(wt)


def _force_request_changes(monkeypatch, output):
    """Mock _run_reviewer to return a REQUEST_CHANGES verdict with the given
    reviewer output, and install recording mocks for _open_pr/_post_pr_comment.
    Returns ``(open_pr, post_comment)`` so callers can assert on them."""
    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None, risk=None):
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)

    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)
    return open_pr, post_comment


def _disable_auto_escalation(monkeypatch):
    """Ensure _auto_escalation_enabled() returns False so the rework-cap
    branch parks rather than escalating, keeping these tests deterministic."""
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


# A REQUEST_CHANGES verdict that carries genuine findings text (so it is NOT
# routed through the empty-findings inconclusive path).
_REQUEST_CHANGES_WITH_FINDINGS = (
    "The error path is untested and the SQL is injectable; add coverage and "
    "parameterize the query.\nVERDICT: REQUEST_CHANGES"
)

# A reviewer output whose ONLY blocking finding is about commit-message
# format, with a backtick-quoted suggested Conventional Commit message - the
# exact shape that triggers the commit-hygiene autofix.
_COMMIT_HYGIENE_FEEDBACK = (
    "The HEAD commit message must be rewritten to a Conventional Commits "
    "message such as `style(backend): collapse trailing whitespace and "
    "rewrite incoherent max-steps comment`, or the two commits squashed "
    "into a single proper Conventional Commit.\nVERDICT: REQUEST_CHANGES"
)


# ---------------------------------------------------------------------------
# (1) Genuine REQUEST_CHANGES with findings -> open PR + post comment.
# ---------------------------------------------------------------------------

def test_genuine_request_changes_opens_pr_and_posts_comment(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """A genuine REQUEST_CHANGES with findings (real worktree) opens the PR
    once and posts a comment whose body contains the findings text AND the
    cycle number; story["pr_url"] is set, status is changes_requested, and
    review_feedback is stored."""
    wt = _init_git_worktree(tmp_path)
    story = _make_story(plan_dir, status="tests_passed", worktree=wt)
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    open_pr, post_comment = _force_request_changes(
        monkeypatch, _REQUEST_CHANGES_WITH_FINDINGS
    )

    result = p.review_story("prc", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    assert result["pr_url"] == "https://gh/pr/1"

    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["pr_url"] == "https://gh/pr/1"
    assert on_disk["review_feedback"] == _REQUEST_CHANGES_WITH_FINDINGS
    assert on_disk["rework_attempts"] == 1

    open_pr.assert_called_once()
    post_comment.assert_called_once()
    # Body is the second positional arg to _post_pr_comment(worktree, body).
    args, _ = post_comment.call_args
    body = args[1]
    assert "injectable" in body  # raw findings present
    assert "cycle 1" in body  # cycle number present


# ---------------------------------------------------------------------------
# (2) _open_pr raises CalledProcessError -> swallowed, no comment, no crash.
# ---------------------------------------------------------------------------

def test_open_pr_raises_called_process_error_is_swallowed(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """If _open_pr raises subprocess.CalledProcessError, review still returns
    status=changes_requested, review_feedback is stored, _post_pr_comment is
    NOT called, and no exception propagates."""
    wt = _init_git_worktree(tmp_path)
    story = _make_story(plan_dir, status="tests_passed", worktree=wt)
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None, risk=None):
        return _REQUEST_CHANGES_WITH_FINDINGS

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(
        p, "_open_pr",
        Mock(side_effect=subprocess.CalledProcessError(1, "gh")),
    )
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    # Must not raise.
    result = p.review_story("prc", "S1")

    assert result["ok"] is True
    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["review_feedback"] == _REQUEST_CHANGES_WITH_FINDINGS
    post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# (2b) Same failure on the APPROVE path's _open_pr call - this one had NO
# try/except at all (unlike the REQUEST_CHANGES path above): a git-push
# failure here propagated out of review_story, through advance_pipeline's
# tests_passed loop, and aborted the whole scheduler tick - silently
# skipping review for every other tests_passed story and merge adjudication
# for the rest of the plan (confirmed live in advance-scheduler.err.log,
# story 30e5f9fc-3681-40db-ae54-dd95387dd1e7, 2026-08-19).
# ---------------------------------------------------------------------------

def test_open_pr_raises_called_process_error_on_approve_path_is_swallowed(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """If _open_pr raises subprocess.CalledProcessError on an APPROVE verdict,
    review_story must not raise, status stays at its pre-review value (NOT
    pr_open), pr_url is never set, and the next tick can retry."""
    wt = _init_git_worktree(tmp_path)
    story = _make_story(plan_dir, status="tests_passed", worktree=wt)
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    monkeypatch.setattr(
        p, "_run_reviewer", lambda wt, br, **k: "Looks good.\nVERDICT: APPROVE"
    )
    monkeypatch.setattr(
        p, "_open_pr",
        Mock(side_effect=subprocess.CalledProcessError(1, "git push")),
    )

    # Must not raise.
    result = p.review_story("prc", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "tests_passed"
    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "tests_passed"
    assert "pr_url" not in on_disk


# ---------------------------------------------------------------------------
# (3) Inconclusive bare REQUEST_CHANGES (no findings) -> no PR, no comment.
# ---------------------------------------------------------------------------

def test_inconclusive_request_changes_does_not_open_pr(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """A bare REQUEST_CHANGES with no findings is inconclusive: status stays
    tests_passed, _open_pr NOT called, _post_pr_comment NOT called."""
    wt = _init_git_worktree(tmp_path)
    story = _make_story(plan_dir, status="tests_passed", worktree=wt)
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    open_pr, post_comment = _force_request_changes(
        monkeypatch, "VERDICT: REQUEST_CHANGES"
    )

    result = p.review_story("prc", "S1")

    assert result["status"] == "tests_passed"
    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "tests_passed"
    open_pr.assert_not_called()
    post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# (4) Commit-hygiene autofix SUCCESS -> tests_passed, no PR, no comment.
# ---------------------------------------------------------------------------

def test_commit_hygiene_autofix_success_does_not_open_pr(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """A commit-message-only finding with a clean tree auto-amends the HEAD
    commit: status becomes tests_passed, and _open_pr/_post_pr_comment are
    NOT called (the autofix-success path must never open a PR or post)."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path)
    story = _make_story(plan_dir, status="tests_passed", worktree=wt)
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    open_pr, post_comment = _force_request_changes(
        monkeypatch, _COMMIT_HYGIENE_FEEDBACK
    )

    result = p.review_story("prc", "S1")

    assert result["status"] == "tests_passed"
    assert result.get("auto_fixed_commit_message") is True
    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "tests_passed"
    open_pr.assert_not_called()
    post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# (5) Rework cap exhausted -> parked, no PR, no comment.
# ---------------------------------------------------------------------------

def test_rework_cap_exhausted_parks_without_pr(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """When the rework cap is exhausted the story parks: status=parked,
    _open_pr NOT called, _post_pr_comment NOT called."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 2)
    wt = _init_git_worktree(tmp_path)
    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt, rework_attempts=1,
    )
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    open_pr, post_comment = _force_request_changes(
        monkeypatch, _REQUEST_CHANGES_WITH_FINDINGS
    )

    result = p.review_story("prc", "S1")

    assert result["status"] == "parked"
    on_disk = _read_story(plan_dir, "prc", "S1")
    assert on_disk["status"] == "parked"
    assert on_disk["rework_attempts"] == 2
    open_pr.assert_not_called()
    post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# (6) Comment uses RAW findings, not the oracle-preservation preamble.
# ---------------------------------------------------------------------------

def test_comment_uses_raw_findings_not_feedback_preamble(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """On an acceptance-bearing story whose oracle is currently PASSING, the
    code prepends an oracle-preservation preamble to ``feedback`` (stored in
    review_feedback) but the PR comment must use the RAW reviewer_output - so
    the preamble string is ABSENT from the comment body while PRESENT in
    review_feedback."""
    wt = _init_git_worktree(tmp_path)
    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        acceptance=[{"path": "test_acceptance.py"}],
    )
    _write_manifest_with_story(plan_dir, "prc", "S1", story)

    # The oracle re-verification reports PASS, which triggers the preamble.
    monkeypatch.setattr(
        p, "_reverify_acceptance",
        lambda s, wt, sk="": {"state": "pass", "error": ""},
    )
    # An acceptance-bearing story uses REWORK_MAX_ATTEMPTS_ORACLE as its
    # rework cap (default 1), which would park on the very first cycle and
    # never reach the changes_requested PR/comment path this test exercises.
    # Raise the cap so the genuine REQUEST_CHANGES lands on changes_requested.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 2)
    _, post_comment = _force_request_changes(
        monkeypatch, _REQUEST_CHANGES_WITH_FINDINGS
    )

    result = p.review_story("prc", "S1")

    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "prc", "S1")
    # review_feedback carries the oracle-preservation preamble.
    assert "acceptance oracle is currently PASSING" in on_disk["review_feedback"]

    post_comment.assert_called_once()
    args, _ = post_comment.call_args
    body = args[1]
    # The comment body contains the raw findings...
    assert "injectable" in body
    # ...but NOT the oracle-preservation preamble.
    assert "acceptance oracle is currently PASSING" not in body
