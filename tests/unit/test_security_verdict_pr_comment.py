"""A high-risk story's security-engineer verdict is recorded on its PR.

The verdict used to live only in the manifest (``security_review_verdict``), so
a reader of the PR saw the ordinary code review and nothing showing the
security pass had run. It is now also posted as a PR comment once the PR is
open. Drives the real ``review_story``; only the external seams (reviewers,
``gh``) are stubbed.
"""
# ruff: noqa: F811 - the plan_dir/agents_dir pytest fixtures are imported from
# the shared helpers module and also used as same-named test-function
# parameters; ruff's F811 flags that standard fixture-sharing pattern.
import subprocess
from pathlib import Path

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
)

_REFERENCE = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()
_PR_URL = "https://gh/pr/1"


def _story(plan_dir, risk, *, with_worktree=True):
    worktree = plan_dir / "wt"
    if with_worktree:
        worktree.mkdir(exist_ok=True)
    return {
        "summary": "Rotate the signing key",
        "status": "tests_passed",
        "worktree": str(worktree),
        "risk": risk,
    }


def _install(monkeypatch, security_reviewer):
    comments = []
    notices = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", security_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: _PR_URL)
    monkeypatch.setattr(p, "_post_pr_comment", lambda wt, body: comments.append((wt, body)))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notices.append(a))
    return comments, notices


def test_approved_high_risk_story_gets_a_security_review_comment(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc1", {"S1": _story(plan_dir, "high")})
    comments, _ = _install(
        monkeypatch, lambda wt, br, **k: "No injection found.\nVERDICT: APPROVE"
    )

    result = p.review_story("svc1", "S1")

    assert result["status"] == "pr_open"
    assert len(comments) == 1
    worktree, body = comments[0]
    assert worktree == str(plan_dir / "wt")
    assert body.startswith("## Security review")
    assert "APPROVE" in body
    assert "No injection found." in body


def test_low_risk_story_gets_no_security_comment_and_no_security_pass(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc2", {"S1": _story(plan_dir, "low")})

    def _must_not_run(wt, br, **k):
        raise AssertionError("the security pass must not run for a low-risk story")

    comments, _ = _install(monkeypatch, _must_not_run)

    result = p.review_story("svc2", "S1")

    assert result["status"] == "pr_open"
    assert comments == []


def test_a_failed_comment_post_does_not_block_the_approved_story(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc3", {"S1": _story(plan_dir, "high")})
    _, notices = _install(monkeypatch, lambda wt, br, **k: "VERDICT: APPROVE")

    def _gh_fails(wt, body):
        raise subprocess.CalledProcessError(1, ["gh", "pr", "comment"])

    monkeypatch.setattr(p, "_post_pr_comment", _gh_fails)

    result = p.review_story("svc3", "S1")

    assert result["status"] == "pr_open"
    story = _read_manifest(plan_dir, "svc3")["stories"]["S1"]
    assert story["pr_url"] == _PR_URL
    assert story["security_review_verdict"] == "APPROVE"
    assert any("security review comment" in str(n) for n in notices)


def test_an_oversized_security_report_is_truncated_in_the_comment(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc4", {"S1": _story(plan_dir, "high")})
    comments, _ = _install(
        monkeypatch, lambda wt, br, **k: "VERDICT: APPROVE\n" + "x" * 50_000
    )

    p.review_story("svc4", "S1")

    body = comments[0][1]
    assert len(body) <= 10_000
    assert "truncated" in body


def test_no_comment_is_posted_when_the_worktree_is_gone(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc5", {"S1": _story(plan_dir, "high", with_worktree=False)})
    comments, _ = _install(monkeypatch, lambda wt, br, **k: "VERDICT: APPROVE")

    result = p.review_story("svc5", "S1")

    assert result["status"] == "pr_open"
    assert comments == []


def test_a_rejecting_security_review_posts_no_security_review_comment(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "svc6", {"S1": _story(plan_dir, "high")})
    comments, _ = _install(
        monkeypatch,
        lambda wt, br, **k: "Blocking: hardcoded credential\nVERDICT: REQUEST_CHANGES",
    )

    result = p.review_story("svc6", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    assert [c for c in comments if c[1].startswith("## Security review")] == []


def test_reference_no_longer_claims_the_security_pass_is_always_claude():
    assert "always uses the Claude backend regardless" not in _REFERENCE


def test_reference_says_the_security_verdict_is_posted_as_a_pr_comment():
    assert "posted as a PR comment" in _REFERENCE
