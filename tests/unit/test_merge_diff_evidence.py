"""Diff evidence for the overlord's high-risk merge ruling (coa-06).

The overlord cannot catch what both reviewers missed without the change
itself, so the MERGE ADJUDICATION prompt carries a ``DIFF EVIDENCE:`` block:
the diff stat, the story's declared ``files``, the changed files outside them,
the existing tests modified or deleted, and a redacted production diff excerpt.

These tests pin:

* each line of the block for in-scope, out-of-scope, test-deletion and new-test
  changes;
* the production excerpt is redacted before it is cut at 12000 characters;
* any git failure, timeout, OSError or missing worktree renders exactly
  ``DIFF EVIDENCE: unavailable``;
* the block sits in the adjudication prompt before the response format;
* the overlord policy and REFERENCE.md document the block.

The worktree is a real git repository with a bare ``origin`` whose HEAD is
set; no network is used.
"""

import subprocess
from pathlib import Path

import pytest

# pre-existing import cycle: pipeline.server must be imported first
import pipeline.server  # noqa: F401
from pipeline import merge as merge_mod
from pipeline import merge_diff_evidence as evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"
TRUNCATION_MARKER = "\n[truncated]"
EXCERPT_HEADER = "production diff (redacted, first 12000 chars):\n"


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _commit(work, *paths_and_text):
    for rel, text in zip(paths_and_text[::2], paths_and_text[1::2]):
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "change")


@pytest.fixture
def repo(tmp_path):
    """A work checkout on a branch, with ``origin/HEAD`` pointing at ``main``."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(
        work,
        "pipeline/app.py", "x = 1\n",
        "tests/test_x.py", "def test_x():\n    assert True\n",
    )
    _git(work, "push", "-u", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    _git(work, "checkout", "-b", "agent/coa-06")
    return work


def _story(work, files=("pipeline/app.py",)):
    story = {"key": "coa-06", "summary": "diff evidence", "worktree": str(work)}
    if files is not None:
        story["files"] = list(files)
    return story


def _line(block, prefix):
    """The first line of *block* that starts with *prefix*."""
    return next(line for line in block.splitlines() if line.startswith(prefix))


# ---------------------------------------------------------------------------
# Scope lines
# ---------------------------------------------------------------------------


def test_in_scope_change_lists_no_outside_files_and_no_test_edits(repo):
    _commit(repo, "pipeline/app.py", "x = 2\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert block.startswith("DIFF EVIDENCE:\n")
    assert "declared files: pipeline/app.py" in block
    assert "outside declared files: (none)" in block
    assert "existing tests modified or deleted: (none)" in block


def test_undeclared_file_change_is_listed_outside_declared_files(repo):
    _commit(repo, "pipeline/app.py", "x = 2\n", "pipeline/extra.py", "y = 1\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert _line(block, "outside declared files:") == (
        "outside declared files: pipeline/extra.py"
    )


def test_missing_files_list_declares_nothing_so_every_change_is_outside(repo):
    _commit(repo, "pipeline/app.py", "x = 2\n")

    block = evidence.render_diff_evidence(_story(repo, files=None))

    assert "declared files: (none)" in block
    assert "outside declared files: pipeline/app.py" in block


def test_empty_files_list_renders_none_for_declared_files(repo):
    _commit(repo, "pipeline/app.py", "x = 2\n")

    block = evidence.render_diff_evidence(_story(repo, files=()))

    assert "declared files: (none)" in block
    assert "outside declared files: pipeline/app.py" in block


# ---------------------------------------------------------------------------
# Existing tests modified or deleted
# ---------------------------------------------------------------------------


def test_deleting_an_existing_test_is_listed(repo):
    _git(repo, "rm", "tests/test_x.py")
    _git(repo, "commit", "-m", "drop test")

    block = evidence.render_diff_evidence(_story(repo))

    assert _line(block, "existing tests modified or deleted:") == (
        "existing tests modified or deleted: tests/test_x.py"
    )


def test_modifying_an_existing_test_is_listed(repo):
    _commit(repo, "tests/test_x.py", "def test_x():\n    assert False\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert _line(block, "existing tests modified or deleted:") == (
        "existing tests modified or deleted: tests/test_x.py"
    )


def test_adding_a_new_test_file_is_not_listed_as_modified(repo):
    _commit(repo, "tests/test_new.py", "def test_new():\n    assert True\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert "existing tests modified or deleted: (none)" in block
    assert "outside declared files: tests/test_new.py" in block


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "test/helpers.py",
        "pipeline/test_b.py",
        "pkg/c_test.py",
        "web/app.test.ts",
        "web/app.spec.js",
    ],
)
def test_test_file_paths_are_recognised(path):
    assert evidence.is_test_file(path)


@pytest.mark.parametrize(
    "path",
    ["pipeline/app.py", "pipeline/testing_utils.py", "pipeline/contest.py"],
)
def test_production_paths_are_not_test_files(path):
    assert not evidence.is_test_file(path)


# ---------------------------------------------------------------------------
# Production diff excerpt
# ---------------------------------------------------------------------------


def test_oversized_production_diff_is_cut_at_12000_characters_and_marked(repo):
    _commit(repo, "pipeline/app.py", "x = 'a'\n" * 4000)

    block = evidence.render_diff_evidence(_story(repo))

    assert block.endswith(TRUNCATION_MARKER)
    excerpt = block.split(EXCERPT_HEADER, 1)[1][: -len(TRUNCATION_MARKER)]
    assert len(excerpt) == evidence.DIFF_EXCERPT_MAX_CHARS == 12000


def test_small_production_diff_is_not_marked_truncated(repo):
    _commit(repo, "pipeline/app.py", "x = 2\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert "[truncated]" not in block
    assert "+x = 2" in block


def test_test_file_content_is_excluded_from_the_production_diff(repo):
    _commit(repo, "tests/test_x.py", "# TESTMARKER\ndef test_x():\n    assert True\n")

    block = evidence.render_diff_evidence(_story(repo))

    assert "TESTMARKER" not in block


def test_secret_in_production_diff_is_redacted_before_the_excerpt(repo):
    _commit(repo, "pipeline/app.py", f'token = "{TOKEN}"\n')

    block = evidence.render_diff_evidence(_story(repo))

    assert "[redacted]" in block
    assert TOKEN not in block


# ---------------------------------------------------------------------------
# Failure: fail closed to a single unavailable line
# ---------------------------------------------------------------------------


def test_missing_worktree_renders_exactly_unavailable(tmp_path):
    story = _story(tmp_path / "does-not-exist")

    assert evidence.render_diff_evidence(story) == "DIFF EVIDENCE: unavailable"


def test_story_without_worktree_renders_exactly_unavailable():
    story = {"key": "coa-06", "files": ["pipeline/app.py"]}

    assert evidence.render_diff_evidence(story) == "DIFF EVIDENCE: unavailable"


def test_git_failure_renders_exactly_unavailable(tmp_path):
    # A repo with no origin remote: ``origin/HEAD`` does not resolve, so git
    # exits non-zero.
    work = tmp_path / "no-origin"
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    _commit(work, "pipeline/app.py", "x = 1\n")

    assert evidence.render_diff_evidence(_story(work)) == (
        "DIFF EVIDENCE: unavailable"
    )


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd="git", timeout=30), OSError("gone")],
)
def test_git_timeout_or_oserror_renders_exactly_unavailable(monkeypatch, repo, error):
    def _raise(*args, **kwargs):
        raise error

    monkeypatch.setattr(evidence.subprocess, "run", _raise)

    assert evidence.render_diff_evidence(_story(repo)) == (
        "DIFF EVIDENCE: unavailable"
    )


# ---------------------------------------------------------------------------
# The adjudication prompt
# ---------------------------------------------------------------------------


def _adjudicate(monkeypatch, story):
    """Run the high-risk adjudication once and return the prompt it sent."""
    # A concluded CI read, so the ruling is made rather than deferred.
    story = {"pr_checks": {"state": "pass", "error": ""}, **story}
    sent = []

    def _overlord(prompt, plan_role_config=None):
        sent.append(prompt)
        return "RULING: park\nRATIONALE: held"

    import pipeline.overlord as overlord_mod

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", _overlord)
    monkeypatch.setattr(merge_mod, "_invoke_pr_checks_gather", lambda *a: None)
    monkeypatch.setattr(merge_mod, "_append_adjudication_decision", lambda *a: None)
    merge_mod._adjudicate_high_risk_merge(story, plan_name="go")
    assert len(sent) == 1
    return sent[0]


def test_prompt_carries_diff_evidence_after_local_checks_and_before_format(
    monkeypatch, repo
):
    _commit(repo, "pipeline/app.py", "x = 2\n")

    prompt = _adjudicate(monkeypatch, _story(repo))

    assert "DIFF EVIDENCE:" in prompt
    assert prompt.index("LOCAL CHECKS:") < prompt.index("DIFF EVIDENCE:")
    assert prompt.index("DIFF EVIDENCE:") < prompt.index(
        "Respond in this exact format"
    )


def test_prompt_renders_unavailable_evidence_and_still_adjudicates(monkeypatch):
    prompt = _adjudicate(monkeypatch, {"key": "coa-06", "files": []})

    assert "DIFF EVIDENCE: unavailable" in prompt
    assert prompt.index("DIFF EVIDENCE: unavailable") < prompt.index(
        "Respond in this exact format"
    )


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_overlord_policy_documents_diff_evidence():
    assert "DIFF EVIDENCE" in (REPO_ROOT / "overlord-policy.md").read_text()


def test_reference_documents_diff_evidence():
    assert "DIFF EVIDENCE" in (REPO_ROOT / "REFERENCE.md").read_text()
