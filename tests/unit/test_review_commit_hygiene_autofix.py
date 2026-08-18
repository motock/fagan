"""Tests for the commit-message-hygiene auto-amend path in ``review_story``.

BACKGROUND (real incident, 2026-08-05, config-surface-a2-cleanup plan, story
eb9ac838, PR #238): a local model implemented a correct fix and got
REQUEST_CHANGES three review cycles in a row; in all three cycles the
reviewer's ONLY Blocking finding was that the HEAD commit message was not a
valid Conventional Commit - zero code or test content was ever wrong after
cycle 1. The story exhausted its rework budget and parked purely over
commit-message hygiene, even though the reviewer's feedback text already
contained a ready-to-use suggested replacement message in backticks.

This story makes the harness do it automatically: when ``review_story`` gets a
REQUEST_CHANGES verdict whose only Blocking finding(s) are about
commit-message format (not file content), and the reviewer's feedback
includes a backtick-quoted suggested replacement message, amend the HEAD
commit directly and re-queue for review - WITHOUT spending a rework_attempts
cycle or redispatching the implementer agent.

These tests are written to fail until the implementation exists. They are
fully standalone (own fixtures/helpers, no cross-file imports of test code) and
mirror the fixture/helper structure of ``test_final_rework_escalation.py``
(read-only reference), per that file's own stated one-file-per-guard
convention. Unlike that file's stories, these tests exercise git directly: a
real git repo is initialized in a tmp_path worktree, a file is committed with
a deliberately bad message, and the story's ``worktree`` field points at it.
``p.review_story`` is called directly (it delegates through the thin lock-guard
wrapper to the real implementation), so these tests exercise the real
registered entry point end-to-end.
"""

import json
import subprocess
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_final_rework_escalation.py so this file is fully
# standalone).
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
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
# Helpers (replicated from the sibling test files - do not import across
# test files).
# ---------------------------------------------------------------------------


def _write_manifest_with_story(plan_dir, plan_name, story_key, story, *,
                               top_level=None):
    """Write a manifest containing a single story, plus optional top-level
    keys."""
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
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


def _init_git_worktree(tmp_path, *, dirty=False, bad_message="wip(x): checkpoint-1"):
    """Create a real git repo in a tmp_path subdir, commit one file with a
    deliberately bad commit message, and return the repo path. If ``dirty`` is
    True, also leave an uncommitted modification to the file so the tree is
    not clean."""
    wt = tmp_path / "wt"
    wt.mkdir()
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


def _head_subject(worktree):
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()


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


# A reviewer output whose ONLY blocking finding is about commit-message
# format (no file-scoped finding), with a backtick-quoted suggested
# Conventional Commit message - the exact incident shape.
_COMMIT_HYGIENE_FEEDBACK = (
    "The HEAD commit message must be rewritten to a Conventional Commits "
    "message such as `style(backend): collapse trailing whitespace and "
    "rewrite incoherent max-steps comment`, or the two commits squashed "
    "into a single proper Conventional Commit.\nVERDICT: REQUEST_CHANGES"
)
_SUGGESTED_SUBJECT = (
    "style(backend): collapse trailing whitespace and "
    "rewrite incoherent max-steps comment"
)

# A reviewer output with a real file-scoped Blocking finding.
_FILE_FINDING_FEEDBACK = (
    "- Blocking: app/backend.py: off-by-one\n"
    "VERDICT: REQUEST_CHANGES"
)

# A reviewer output with no file-scoped finding but also no backtick-quoted
# suggested message.
_NO_SUGGESTION_FEEDBACK = (
    "The HEAD commit message is not a valid Conventional Commit; please "
    "rewrite it.\nVERDICT: REQUEST_CHANGES"
)


# ---------------------------------------------------------------------------
# (a) Happy path: commit-message-only finding + clean tree -> auto-amend.
# ---------------------------------------------------------------------------

def test_commit_message_only_finding_amends_head_commit(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """REQUEST_CHANGES whose reviewer_output has no file-scoped Blocking
    finding and a backtick-quoted suggested Conventional Commit message, with
    a clean git tree: the HEAD commit is amended to the suggested subject,
    status becomes tests_passed, rework_attempts is NOT incremented, and
    commit_hygiene_autofix_attempts becomes 1."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path)
    assert _head_subject(wt) == "wip(x): checkpoint-1"

    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        backend="local", model="gpt-oss-20b",
    )
    _write_manifest_with_story(plan_dir, "cha", "S1", story)

    notify_calls = []
    monkeypatch.setattr(
        p, "_notify_user",
        lambda plan_name, msg: notify_calls.append((plan_name, msg)),
    )
    open_pr, post_comment = _force_request_changes(
        monkeypatch, _COMMIT_HYGIENE_FEEDBACK
    )

    result = p.review_story("cha", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "tests_passed"
    assert result.get("auto_fixed_commit_message") is True
    # The HEAD commit message was amended to the suggested subject.
    assert _head_subject(wt) == _SUGGESTED_SUBJECT
    on_disk = _read_story(plan_dir, "cha", "S1")
    assert on_disk["status"] == "tests_passed"
    assert on_disk.get("rework_attempts", 0) == 0, (
        "rework_attempts must NOT be incremented on an auto-amend"
    )
    assert on_disk.get("commit_hygiene_autofix_attempts") == 1
    # A notification was emitted.
    assert any("auto-amended" in msg for _, msg in notify_calls), notify_calls
    # The autofix-success path must NOT open a PR or post a comment.
    open_pr.assert_not_called()
    post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# (b) Dirty tree -> do NOT amend, fall through to normal rework path.
# ---------------------------------------------------------------------------

def test_dirty_tree_does_not_amend_falls_through_to_rework(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """Same scenario as (a) but the worktree has an uncommitted modified
    file (dirty tree): the commit is NOT amended (original bad message
    unchanged), and the story falls through to the normal rework path
    (status == changes_requested, rework_attempts incremented)."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path, dirty=True)
    assert _head_subject(wt) == "wip(x): checkpoint-1"

    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        backend="local", model="gpt-oss-20b",
    )
    _write_manifest_with_story(plan_dir, "cha", "S1", story)

    _force_request_changes(monkeypatch, _COMMIT_HYGIENE_FEEDBACK)

    result = p.review_story("cha", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    # Commit message unchanged.
    assert _head_subject(wt) == "wip(x): checkpoint-1"
    on_disk = _read_story(plan_dir, "cha", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk.get("rework_attempts", 0) == 1, (
        "rework_attempts must be incremented on the normal rework path"
    )
    # The autofix counter key must NOT be created when the guard fails.
    assert "commit_hygiene_autofix_attempts" not in on_disk


# ---------------------------------------------------------------------------
# (c) Real file-scoped Blocking finding -> completely unaffected.
# ---------------------------------------------------------------------------

def test_file_scoped_finding_is_unaffected_by_autofix(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """A REQUEST_CHANGES verdict with a real file-scoped Blocking finding is
    completely unaffected: commit message unchanged, normal rework path taken,
    and the commit_hygiene_autofix_attempts key is never created."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path)
    assert _head_subject(wt) == "wip(x): checkpoint-1"

    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        backend="local", model="gpt-oss-20b",
    )
    _write_manifest_with_story(plan_dir, "cha", "S1", story)

    _force_request_changes(monkeypatch, _FILE_FINDING_FEEDBACK)

    result = p.review_story("cha", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    # Commit message unchanged.
    assert _head_subject(wt) == "wip(x): checkpoint-1"
    on_disk = _read_story(plan_dir, "cha", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk.get("rework_attempts", 0) == 1
    assert "commit_hygiene_autofix_attempts" not in on_disk


# ---------------------------------------------------------------------------
# (d) Cap reached (commit_hygiene_autofix_attempts == 2) -> fall through.
# ---------------------------------------------------------------------------

def test_autofix_cap_reached_falls_through_to_rework(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """``commit_hygiene_autofix_attempts`` already at 2 (cap reached): even
    with a clean tree and a valid suggested message, fall through to the
    normal rework path instead of amending again."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path)
    assert _head_subject(wt) == "wip(x): checkpoint-1"

    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        backend="local", model="gpt-oss-20b",
        commit_hygiene_autofix_attempts=2,
    )
    _write_manifest_with_story(plan_dir, "cha", "S1", story)

    _force_request_changes(monkeypatch, _COMMIT_HYGIENE_FEEDBACK)

    result = p.review_story("cha", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    # Commit message unchanged.
    assert _head_subject(wt) == "wip(x): checkpoint-1"
    on_disk = _read_story(plan_dir, "cha", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk.get("rework_attempts", 0) == 1
    # The cap is not exceeded.
    assert on_disk.get("commit_hygiene_autofix_attempts") == 2


# ---------------------------------------------------------------------------
# (e) No suggested message in feedback -> fall through to normal rework.
# ---------------------------------------------------------------------------

def test_missing_suggested_message_falls_through_to_rework(
    plan_dir, agents_dir, tmp_path, monkeypatch,
):
    """The reviewer's suggested message is missing (no backticks in the
    feedback) even though there's no file-scoped finding: fall through to the
    normal rework path unchanged."""
    _disable_auto_escalation(monkeypatch)
    wt = _init_git_worktree(tmp_path)
    assert _head_subject(wt) == "wip(x): checkpoint-1"

    story = _make_story(
        plan_dir, status="tests_passed", worktree=wt,
        backend="local", model="gpt-oss-20b",
    )
    _write_manifest_with_story(plan_dir, "cha", "S1", story)

    _force_request_changes(monkeypatch, _NO_SUGGESTION_FEEDBACK)

    result = p.review_story("cha", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    # Commit message unchanged.
    assert _head_subject(wt) == "wip(x): checkpoint-1"
    on_disk = _read_story(plan_dir, "cha", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk.get("rework_attempts", 0) == 1
    assert "commit_hygiene_autofix_attempts" not in on_disk


# ---------------------------------------------------------------------------
# (f) Import: _extract_suggested_commit_message is imported into server.
# ---------------------------------------------------------------------------

def test_server_imports_extract_suggested_commit_message():
    """pipeline.server must import _extract_suggested_commit_message from
    pipeline.parsers alongside the existing _extract_blocking_finding_files
    and _has_review_findings imports."""
    from pipeline import parsers
    assert hasattr(p, "_extract_suggested_commit_message")
    # It is the same object as the parsers function.
    assert p._extract_suggested_commit_message is parsers._extract_suggested_commit_message