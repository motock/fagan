"""Tests for the pipeline MCP server: ingest_plan re-ingest merging and the review gate + auto-PR.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess
from datetime import datetime, timezone

import pytest

from app import (
    backend,
    role_registry,
)
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    SAMPLE_USAGE_TEXT,
    _clear_caches,
    _fake_plane,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _story,
    _write_manifest,
    agents_dir,
    plan_dir,
    usage_state_path,
    worktree_root,
)

# ---------- Security review gate ----------

def test_review_story_high_risk_calls_security_reviewer(plan_dir, agents_dir, monkeypatch):
    """High-risk stories must invoke the security-engineer reviewer in addition to code-reviewer."""
    _write_manifest(plan_dir, "secgate", {
        "S1": {"summary": "Add auth", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    security_calls = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: (security_calls.append(1), "VERDICT: APPROVE")[1])
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("secgate", "S1")
    assert len(security_calls) == 1, "security-engineer reviewer must be called for high-risk stories"


def test_review_story_high_risk_both_approve_opens_pr(plan_dir, agents_dir, monkeypatch):
    """High-risk story approved by both reviewers proceeds to pr_open."""
    _write_manifest(plan_dir, "secboth", {
        "S1": {"summary": "Crypto change", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    result = p.review_story("secboth", "S1")
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"


def test_review_story_high_risk_security_request_changes_blocks_merge(plan_dir, agents_dir, monkeypatch):
    """If security-engineer requests changes, story must NOT go to pr_open even if code-reviewer approves."""
    _write_manifest(plan_dir, "secblock", {
        "S1": {"summary": "Token store", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: "Security issue found.\nVERDICT: REQUEST_CHANGES")

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened when security reviewer blocks")
    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("secblock", "S1")
    assert result["verdict"] != "APPROVE", "combined verdict must not be APPROVE when security rejects"
    assert result["status"] in ("changes_requested", "parked")


def test_review_story_high_risk_security_verdict_recorded(plan_dir, agents_dir, monkeypatch):
    """security_review_verdict is persisted in the manifest for audit purposes."""
    _write_manifest(plan_dir, "secrecord", {
        "S1": {"summary": "RBAC impl", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("secrecord", "S1")
    story = _read_manifest(plan_dir, "secrecord")["stories"]["S1"]
    assert story.get("security_review_verdict") == "APPROVE"


def test_review_story_low_risk_skips_security_reviewer(plan_dir, agents_dir, monkeypatch):
    """Low-risk stories must NOT invoke the security-engineer reviewer."""
    _write_manifest(plan_dir, "secskip", {
        "S1": {"summary": "Fix typo", "status": "in_progress",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    security_calls = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: (security_calls.append(1), "VERDICT: APPROVE")[1])
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("secskip", "S1")
    assert len(security_calls) == 0, "security reviewer must NOT be called for low-risk stories"


def test_run_security_reviewer_always_uses_claude_backend_even_under_local_review(agents_dir, monkeypatch):
    """T11/T12: the security-engineer pass is the one place "cloud only as
    reviewer" and "security review needs human-grade scrutiny" are the same
    requirement - it must never silently run on the local reviewer just
    because PIPELINE_BACKEND_REVIEW=local is set for ordinary review. Stub
    the registry with NO roles.security entry so this exercises
    resolve_role's hardcoded default_provider="claude" fallback - a code
    invariant, not a value that changes when model_registry.json's other
    roles get reconfigured."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    fake_registry = {"providers": {}, "roles": {}}
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            return "VERDICT: APPROVE"

    def _fake_get_backend(role, name=None):
        captured["role"] = role
        captured["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._run_security_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["role"] == "review"
    assert captured["name"] == "claude"


def test_run_security_reviewer_does_not_run_test_suite(agents_dir, monkeypatch):
    """The security reviewer reviews the diff for security issues and trusts
    CI (tests_passed already gated entry); it must not re-run the test
    suite -- that is pure duplicate spend, same rationale as the ordinary
    reviewer."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_security_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"]
    assert "Run the test suite" not in prompt
    assert "agent/some-branch" in prompt


def test_run_security_reviewer_incremental_review_scopes_to_since_sha(agents_dir, monkeypatch):
    """On a rework, the security reviewer also reviews only the new diff
    since the last review, not the whole branch from zero."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_security_reviewer("/tmp/some-worktree", "agent/some-branch", since_sha="deadbee")

    prompt = captured["prompt"]
    assert "git diff deadbee..HEAD" in prompt
    assert "Run the test suite" not in prompt


def test_run_security_reviewer_routes_via_plan_role_config_security(agents_dir, monkeypatch):
    """The security-engineer pass is role-routable: a plan's
    role_config.security overrides the Claude default, so a high-risk
    story can clear security review on a configured non-Claude backend
    (e.g. ollama/glm) instead of dead-ending when Claude is unavailable.
    Unconfigured, it still resolves to Claude (covered by
    test_run_security_reviewer_always_uses_claude_backend_even_under_local_review).

    Stubs role_registry.load_registry with a synthetic fixture rather than
    hitting the real model_registry.json: the friendly name "glm" resolves
    to whatever tag that file's providers.ollama.models.glm.tag currently
    holds, and asserting that live value here ties this test to today's
    registry contents (CLAUDE.md's "Testing Configuration-Driven Logic")."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    monkeypatch.setattr(
        role_registry, "load_registry",
        lambda *a, **k: {"providers": {"ollama": {"models": {"glm": {"tag": "glm-test-tag:cloud"}}}}},
    )
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    def _fake_get_backend(role, name=None):
        captured["role"] = role
        captured["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._run_security_reviewer(
        "/tmp/some-worktree", "agent/some-branch",
        plan_role_config={"security": {"provider": "ollama", "model": "glm"}},
    )

    assert captured["role"] == "review"
    assert captured["name"] == "ollama"
    assert captured["model"] == "glm-test-tag:cloud"


# ---------- Reviewer self-fix (APPROVE_WITH_FIX) ----------

def _init_auto_fix_repo(path):
    p.subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _head_sha(path):
    return p.subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def test_verify_reviewer_auto_fix_rejects_non_low_risk_without_running_anything(
    tmp_path, monkeypatch,
):
    """The risk check is a mechanical veto, checked BEFORE anything else -
    a high-risk story never even gets its diff stat computed or its tests
    run, regardless of what the reviewer claims."""
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "high"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "risk" in feedback.lower()


def test_verify_reviewer_auto_fix_rejects_when_no_baseline_sha(tmp_path):
    """A missing before_sha (e.g. the worktree didn't exist/wasn't a git
    repo when review started) is unverifiable - fail closed rather than
    trusting an unbounded diff."""
    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", None,
    )
    assert verdict == "REQUEST_CHANGES"
    assert "could not be verified" in feedback.lower()


def test_verify_reviewer_auto_fix_rejects_when_no_new_commit(tmp_path, monkeypatch):
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "never actually committed" in feedback.lower()


def test_verify_reviewer_auto_fix_rejects_when_too_many_files_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "REVIEWER_AUTO_FIX_MAX_FILES", 1)
    monkeypatch.setattr(p, "REVIEWER_AUTO_FIX_MAX_LINES", 100)
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    p.subprocess.run(["git", "commit", "-aq", "-m", "fix"], cwd=tmp_path, check=True)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "2 file" in feedback


def test_verify_reviewer_auto_fix_rejects_when_too_many_lines_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "REVIEWER_AUTO_FIX_MAX_FILES", 5)
    monkeypatch.setattr(p, "REVIEWER_AUTO_FIX_MAX_LINES", 3)
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\ny = 3\nz = 4\nw = 5\n")
    p.subprocess.run(["git", "commit", "-aq", "-m", "fix"], cwd=tmp_path, check=True)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "changed line" in feedback.lower()


def test_verify_reviewer_auto_fix_rejects_when_tests_fail(tmp_path, monkeypatch):
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    p.subprocess.run(["git", "commit", "-aq", "-m", "fix"], cwd=tmp_path, check=True)

    marker = "__auto_fix_test_marker__"
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, [marker, "pytest"]))
    real_run = p.subprocess.run

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == marker:
            return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")
        return real_run(cmd, **kw)
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "1 failed" in feedback


def test_verify_reviewer_auto_fix_approves_small_verified_fix(tmp_path, monkeypatch):
    _init_auto_fix_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    before_sha = _head_sha(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    p.subprocess.run(["git", "commit", "-aq", "-m", "fix"], cwd=tmp_path, check=True)

    marker = "__auto_fix_test_marker__"
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, [marker, "pytest"]))
    real_run = p.subprocess.run

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == marker:
            return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")
        return real_run(cmd, **kw)
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX - trivial fix", before_sha,
    )

    assert verdict == "APPROVE"
    assert "trivial fix" in feedback
    assert "harness-verified" in feedback


def test_review_story_approve_with_fix_verified_opens_pr(plan_dir, agents_dir, monkeypatch):
    """DYNAMIC integration check: review_story itself must call
    _verify_reviewer_auto_fix and honor its result, not just parse the raw
    VERDICT line - a verified self-fix proceeds exactly like an ordinary
    APPROVE (opens a PR, clears rework state)."""
    _write_manifest(plan_dir, "autofixok", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE_WITH_FIX")
    monkeypatch.setattr(p, "_verify_reviewer_auto_fix",
                        lambda wt, story, output, before_sha: ("APPROVE", "verified fix"))
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    result = p.review_story("autofixok", "S1")

    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    story = _read_manifest(plan_dir, "autofixok")["stories"]["S1"]
    assert story["status"] == "pr_open"


def test_review_story_approve_with_fix_failed_verification_becomes_changes_requested(
    plan_dir, agents_dir, monkeypatch,
):
    """The mirror case: an APPROVE_WITH_FIX that FAILS harness verification
    (too many files changed, the full suite broke, etc.) must downgrade to
    REQUEST_CHANGES exactly like an ordinary rejection - no PR opens, and it
    counts against the rework budget so a story can't loop on unverified
    self-fixes forever."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "autofixbad", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE_WITH_FIX")
    monkeypatch.setattr(p, "_verify_reviewer_auto_fix",
                        lambda wt, story, output, before_sha: (
                            "REQUEST_CHANGES", "self-fix touched too many files"))

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened when auto-fix verification fails")
    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("autofixbad", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    story = _read_manifest(plan_dir, "autofixbad")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert "too many files" in story["review_feedback"]
    assert story["rework_attempts"] == 1


def test_review_story_passes_risk_to_run_reviewer(plan_dir, agents_dir, monkeypatch):
    """review_story must thread the story's risk tier into _run_reviewer so
    the self-fix option is only ever offered when actually eligible - not
    just tolerated by signature."""
    captured = {}
    _write_manifest(plan_dir, "riskthread", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                        since_sha=None, risk="low"):
        captured["risk"] = risk
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("riskthread", "S1")

    assert captured["risk"] == "high"


def test_review_story_threads_last_reviewed_sha_as_since_sha(plan_dir, agents_dir, monkeypatch):
    """On a rework review (last_reviewed_sha recorded from the prior
    REQUEST_CHANGES), review_story must pass it to _run_reviewer as
    since_sha so the reviewer scopes to the new diff only -- not re-read
    the whole branch from zero."""
    _write_manifest(plan_dir, "incr", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "last_reviewed_sha": "abc123def"},
    })
    captured = {}

    def _capture(wt, br, **kwargs):
        captured["kwargs"] = kwargs
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _capture)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("incr", "S1")

    assert captured["kwargs"].get("since_sha") == "abc123def"
    # The removed acceptance= kwarg must no longer be threaded.
    assert "acceptance" not in captured["kwargs"]


def test_review_story_first_review_passes_no_since_sha(plan_dir, agents_dir, monkeypatch):
    """First review (no last_reviewed_sha): since_sha is None, so the
    reviewer covers the full branch diff."""
    _write_manifest(plan_dir, "first", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    captured = {}

    def _capture(wt, br, **kwargs):
        captured["kwargs"] = kwargs
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _capture)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("first", "S1")

    assert captured["kwargs"].get("since_sha") is None


def test_run_reviewer_ordinary_review_still_honors_local_backend_setting(agents_dir, monkeypatch):
    """Regression guard for T12: forcing the security pass onto Claude must
    not leak into the ordinary code-reviewer pass, which should still honor
    PIPELINE_BACKEND_REVIEW=local exactly as before.

    Stubs role_registry.load_registry with a synthetic fixture declaring a
    roles.review entry (pattern already used elsewhere in this file, e.g.
    test_run_security_reviewer_routes_via_plan_role_config_security),
    rather than depending on the live model_registry.json's roles.review
    entry — .claude/rules/testing-config-gates.md: test the resolution
    logic, not today's configured values."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    fake_registry = {"providers": {}, "roles": {"review": {"provider": "claude", "model": "sonnet"}}}
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            return "VERDICT: APPROVE"

    def _fake_get_backend(role, name=None):
        captured["role"] = role
        captured["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["role"] == "review"
    # The stubbed registry carries an explicit "review" entry (claude/
    # sonnet), so _run_reviewer passes the env-resolved provider ("local",
    # since PIPELINE_BACKEND_REVIEW wins) explicitly through to get_backend
    # instead of leaving it to get_backend's own internal lookup - same
    # real backend, just resolved one layer earlier now.
    assert captured["name"] == "local"


# ---------- Per-plan repo_root ----------
def test_repo_root_for_returns_manifest_value_when_present(plan_dir):
    _write_manifest(plan_dir, "rr1", {})
    manifest_path = plan_dir / "rr1.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = "/some/specific/repo"
    manifest_path.write_text(json.dumps(manifest))

    assert p._repo_root_for("rr1") == p.Path("/some/specific/repo")


def test_repo_root_for_falls_back_to_global_when_absent_in_manifest(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "rr2", {})
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    assert p._repo_root_for("rr2") == tmp_path


def test_repo_root_for_falls_back_when_no_manifest_exists(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    assert p._repo_root_for("does-not-exist") == tmp_path


def test_scoped_repo_root_sets_and_restores(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "sr1", {})
    manifest_path = plan_dir / "sr1.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(tmp_path / "the-repo")
    manifest_path.write_text(json.dumps(manifest))

    original = p.Path("/original/repo")
    monkeypatch.setattr(p, "REPO_ROOT", original)

    with p._scoped_repo_root("sr1") as scoped:
        assert scoped == tmp_path / "the-repo"
        assert p.REPO_ROOT == tmp_path / "the-repo"
    assert p.REPO_ROOT == original


def test_scoped_repo_root_restores_on_exception(plan_dir, monkeypatch, tmp_path):
    _write_manifest(plan_dir, "sr2", {})
    manifest_path = plan_dir / "sr2.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(tmp_path / "the-repo")
    manifest_path.write_text(json.dumps(manifest))

    original = p.Path("/original/repo")
    monkeypatch.setattr(p, "REPO_ROOT", original)

    with pytest.raises(RuntimeError), p._scoped_repo_root("sr2"):
        raise RuntimeError("boom")
    assert p.REPO_ROOT == original


def test_default_branch_does_not_leak_cache_across_repos(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_default_branch_cache", {})
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"

    def _fake_run(cmd, cwd=None, **kwargs):
        class Result:
            returncode = 0
            stdout = ("origin/feature-a\n" if cwd == repo_a
                       else "origin/feature-b\n")
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    monkeypatch.setattr(p, "REPO_ROOT", repo_a)
    branch_a = p._default_branch()
    monkeypatch.setattr(p, "REPO_ROOT", repo_b)
    branch_b = p._default_branch()

    assert branch_a == "feature-a"
    assert branch_b == "feature-b"


def test_ingest_plan_carries_repo_root_into_manifest(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "epics": [{"summary": "E1", "stories": [_story()]}],
        "repo_root": str(tmp_path),
    }
    (plan_dir / "rrplan.json").write_text(json.dumps(plan))
    result = p.ingest_plan("rrplan")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "rrplan.manifest.json").read_text())
    assert manifest["repo_root"] == str(tmp_path)


def test_dispatch_story_uses_manifest_repo_root_for_git_commands(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    real_repo = tmp_path / "real-repo"
    _write_manifest(plan_dir, "rrds", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "rrds.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))

    cwds_used = []

    def _fake_run(cmd, cwd=None, **kwargs):
        cwds_used.append(cwd)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(123))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    p.dispatch_story("rrds", "S1")

    assert any(c == real_repo for c in cwds_used), cwds_used
    assert all(c != p.Path("/wrong/default/repo") for c in cwds_used)
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_dispatch_story_records_resolved_model_on_manifest(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """dispatch_story writes the RESOLVED model the agent actually boots with
    to story['dispatched_model'] so the dashboard can show what ran (e.g.
    minimax-m3:cloud under the local backend) instead of the plan's declared
    tier ('sonnet'). The declared story['model'] is left unchanged (it's a
    routing hint the plan specified)."""
    real_repo = tmp_path / "real-repo"
    _write_manifest(plan_dir, "drm", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "model": "sonnet"},
    })
    manifest_path = plan_dir / "drm.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, cwd=None, **kw: _R())
    monkeypatch.setattr(backend.subprocess, "Popen", lambda argv, **kw: _FakeProc(123))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "minimax-m3:cloud")
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_SONNET", raising=False)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    p.dispatch_story("drm", "S1")

    story = json.loads(manifest_path.read_text())["stories"]["S1"]
    assert story["model"] == "sonnet"                       # declared tier unchanged
    assert story["dispatched_model"] == "minimax-m3:cloud"  # actual model recorded


def test_dispatch_story_records_dispatched_at_timestamp(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """dispatch_story records when the agent was launched so check_story_status
    can bound how long a dispatch is allowed to run before its subprocess is
    treated as hung (see the watchdog tests on check_story_status)."""
    real_repo = tmp_path / "real-repo"
    _write_manifest(plan_dir, "dat", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "dat.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, cwd=None, **kw: _R())
    monkeypatch.setattr(backend.subprocess, "Popen", lambda argv, **kw: _FakeProc(123))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    before = datetime.now(timezone.utc)
    p.dispatch_story("dat", "S1")
    after = datetime.now(timezone.utc)

    story = json.loads(manifest_path.read_text())["stories"]["S1"]
    dispatched_at = datetime.fromisoformat(story["dispatched_at"])
    assert before <= dispatched_at <= after


def test_advance_pipeline_merge_uses_plan_repo_root(plan_dir, monkeypatch, tmp_path):
    real_repo = tmp_path / "real-repo"
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    _write_manifest(plan_dir, "rrmerge", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": str(tmp_path / "wt")},
    })
    manifest_path = plan_dir / "rrmerge.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    cleanup_cwds = []

    def _fake_run(cmd, cwd=None, **kwargs):
        if cmd[:2] == ["git", "worktree"] or cmd[:2] == ["git", "branch"] or cmd[:2] == ["git", "push"]:
            cleanup_cwds.append(cwd)
        class Result:
            returncode = 0
            stdout = "merged\n"
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.advance_pipeline("rrmerge")

    assert cleanup_cwds, "expected cleanup git commands to run"
    assert all(c == real_repo for c in cleanup_cwds)
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_request_decision_loads_policy_override_from_plan_repo_root(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    (real_repo / ".overlord-policy.md").write_text("Per-repo override text.")
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    monkeypatch.setattr(p, "POLICY_PATH", tmp_path / "nonexistent-global-policy.md")
    _write_manifest(plan_dir, "rrdec", {})
    manifest_path = plan_dir / "rrdec.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    captured_prompt = {}

    def _fake_invoke(prompt, **k):
        captured_prompt["text"] = prompt
        return "RULING: x\nTIER: routine\nRISK: low\nRATIONALE: y\nNOTIFY_USER: no\n"

    monkeypatch.setattr(p, "_invoke_overlord", _fake_invoke)

    p.request_decision("rrdec", "S1", "q", ["a"])

    assert "Per-repo override text." in captured_prompt["text"]
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


# ---------- Usage probe ----------
def test_parse_usage_output_extracts_session_and_week():
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT)
    assert result["session_pct"] == 9
    assert result["session_reset"] == "Jun 18 at 11:59am (America/Chicago)"
    assert result["week_pct"] == 48
    assert result["week_reset"] == "Jun 23 at 9am (America/Chicago)"


def test_parse_usage_output_handles_100_percent():
    text = (
        "Current session: 100% used · resets Jun 18 at 11:59am (America/Chicago)\n"
        "Current week (all models): 100% used · resets Jun 23 at 9am (America/Chicago)\n"
    )
    result = p._parse_usage_output(text)
    assert result["session_pct"] == 100
    assert result["week_pct"] == 100


def test_parse_usage_output_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        p._parse_usage_output("some unexpected format with no usage lines")


def test_run_usage_probe_parses_and_stamps_checked_at(monkeypatch):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p._run_usage_probe()

    assert calls == [["claude", "-p", "/cost", "--output-format", "json"]]
    assert result["session_pct"] == 9
    assert result["week_pct"] == 48
    assert "checked_at" in result


def test_run_usage_probe_raises_on_invalid_json(monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "not json"
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        p._run_usage_probe()


def test_write_and_read_usage_state_roundtrip(usage_state_path):
    p._write_usage_state({"session_pct": 9, "week_pct": 48, "checked_at": "x"})
    assert p._read_usage_state() == {"session_pct": 9, "week_pct": 48, "checked_at": "x"}


def test_read_usage_state_missing_file_returns_empty_dict(usage_state_path):
    assert p._read_usage_state() == {}


# ---------- Atomic writes ----------

def test_atomic_write_json_leaves_original_intact_on_rename_failure(tmp_path, monkeypatch):
    """If os.replace raises, the original file must be left untouched."""
    target = tmp_path / "data.json"
    original = {"key": "original_value"}
    target.write_text(json.dumps(original))

    def bad_replace(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("os.replace", bad_replace)
    with pytest.raises(OSError):
        p._atomic_write_json(target, {"key": "new_value"})

    assert json.loads(target.read_text()) == original


def test_atomic_write_json_no_tmp_left_on_rename_failure(tmp_path, monkeypatch):
    """On rename failure, no stray .tmp file remains in the directory."""
    target = tmp_path / "data.json"
    target.write_text("{}")

    def bad_replace(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("os.replace", bad_replace)
    with pytest.raises(OSError):
        p._atomic_write_json(target, {"x": 1})

    leftovers = [f for f in tmp_path.iterdir() if f != target]
    assert leftovers == [], f"stray tmp files: {leftovers}"


def test_atomic_write_json_produces_valid_json(tmp_path):
    target = tmp_path / "out.json"
    payload = {"a": [1, 2], "b": {"nested": True}}
    p._atomic_write_json(target, payload)
    assert json.loads(target.read_text()) == payload


def test_atomic_write_json_creates_file_when_missing(tmp_path):
    target = tmp_path / "new.json"
    p._atomic_write_json(target, {"hello": "world"})
    assert json.loads(target.read_text()) == {"hello": "world"}


def test_write_usage_state_uses_atomic_write(tmp_path, monkeypatch, usage_state_path):
    """_write_usage_state must not leave a partial file on rename failure."""
    calls = []

    real_atomic = pusage._atomic_write_json

    def tracking_atomic(path, obj):
        calls.append(path)
        real_atomic(path, obj)

    monkeypatch.setattr(pusage, "_atomic_write_json", tracking_atomic)
    p._write_usage_state({"session_pct": 5})
    assert any(str(usage_state_path) in str(c) for c in calls), "usage-state write did not go through _atomic_write_json"


def test_append_journal_uses_atomic_write(tmp_path, plan_dir, monkeypatch):
    """_append_journal must route through _atomic_write_json."""
    calls = []
    real_atomic = ppers._atomic_write_json

    def tracking_atomic(path, obj):
        calls.append(path)
        real_atomic(path, obj)

    monkeypatch.setattr(ppers, "_atomic_write_json", tracking_atomic)
    p._append_journal("myplan", "story-1", {"event": "checkpoint"})
    assert any(".journal.json" in str(c) for c in calls), "journal write did not go through _atomic_write_json"


