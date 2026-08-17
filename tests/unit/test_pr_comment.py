"""Tests for the PR review-comment helpers in pipeline.pr.

Covers `_format_review_comment` (markdown body builder) and
`_post_pr_comment` (gh CLI comment poster). Both are external-boundary
helpers: `_post_pr_comment` spawns `gh`, so tests monkeypatch
`pipeline.pr.subprocess.run` rather than hitting a real remote.
"""

import subprocess

import pytest

import pipeline.pr as p

# ---------------------------------------------------------------------------
# _post_pr_comment
# ---------------------------------------------------------------------------

def test_post_pr_comment_invokes_gh_with_exact_argv_and_cwd(monkeypatch):
    """_post_pr_comment must call subprocess.run with the exact argv
    `["gh", "pr", "comment", "--body", body]` and cwd=worktree, with
    check/capture_output/text set."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        class Result:
            stdout = ""
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    worktree = "/some/worktree"
    body = "## ⚠️ Changes requested — automated review (cycle 1)\n\nneeds work"
    p._post_pr_comment(worktree, body)

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["gh", "pr", "comment", "--body", body]
    assert kwargs["cwd"] == worktree
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_post_pr_comment_propagates_called_process_error(monkeypatch):
    """A failing `gh` call must raise subprocess.CalledProcessError; the
    helper must not swallow it (the caller handles it best-effort)."""
    def _fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="gh: not logged in",
        )

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        p._post_pr_comment("/some/worktree", "body text")

    assert excinfo.value.returncode == 1
    assert "gh: not logged in" in (excinfo.value.stderr or "")


# ---------------------------------------------------------------------------
# _format_review_comment
# ---------------------------------------------------------------------------

def test_format_review_comment_includes_cycle_and_findings():
    body = p._format_review_comment("SQL is injectable; add coverage.", 2)
    assert "cycle 2" in body
    assert "SQL is injectable; add coverage." in body


def test_format_review_comment_header_line():
    body = p._format_review_comment("some finding", 3)
    assert body.startswith("## ⚠️ Changes requested — automated review (cycle 3)")


def test_format_review_comment_findings_verbatim():
    """The findings text must appear verbatim, with no added transformation
    (no wrapping, no quoting, no prefix)."""
    findings = "line one\nline two\n  indented"
    body = p._format_review_comment(findings, 1)
    assert findings in body


def test_format_review_comment_blank_line_between_header_and_findings():
    body = p._format_review_comment("finding", 1)
    header = "## ⚠️ Changes requested — automated review (cycle 1)"
    assert body.startswith(header + "\n\n")


def test_format_review_comment_empty_findings_does_not_crash():
    """Boundary: empty findings string must not crash and must still include
    the header."""
    body = p._format_review_comment("", 1)
    assert "cycle 1" in body
    assert body.startswith("## ⚠️ Changes requested — automated review (cycle 1)")


def test_format_review_comment_cycle_zero_and_one():
    """Boundary: cycle is 1-based; both 0 and 1 must be rendered literally."""
    assert "cycle 0" in p._format_review_comment("x", 0)
    assert "cycle 1" in p._format_review_comment("x", 1)


# ---------------------------------------------------------------------------
# __all__ exports
# ---------------------------------------------------------------------------

def test_pr_module_exports_new_helpers():
    """Both new helpers must be importable from the module and listed in
    __all__."""
    assert hasattr(p, "_format_review_comment")
    assert hasattr(p, "_post_pr_comment")
    assert "_format_review_comment" in p.__all__
    assert "_post_pr_comment" in p.__all__
