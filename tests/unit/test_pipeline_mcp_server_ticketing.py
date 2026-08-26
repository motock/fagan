"""Tests for the pipeline MCP server: logging hygiene, Plane-unconfigured behavior, and the TicketProvider abstraction.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import json
import os

import pytest

from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _explode_plane,
    _fake_plane,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _plane_disabled,
    _read_manifest,
    _story,
    agents_dir,
    plan_dir,
)

# ---------- ingest_plan re-ingest merges instead of clobbering (T1) ----------
# Re-running ingest_plan with only_epics scoped to a newly-added epic used to
# replace the whole manifest, silently deleting every previously-ingested
# story's status/pr_url/history (2026-07-07 web-client-epic retro, incident
# #2: 43 tracked stories -> 10 after one only_epics call).

def test_ingest_plan_reingest_preserves_stories_from_untouched_epics(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "E1", "stories": [_story(key="S1")]},
            {"summary": "E2", "stories": [_story(key="S2")]},
        ],
    }
    (plan_dir / "reingest.json").write_text(json.dumps(plan))

    first = p.ingest_plan("reingest")
    assert first["ok"] is True
    manifest = _read_manifest(plan_dir, "reingest")
    # Simulate real progress recorded against S1 by later pipeline activity.
    manifest["stories"]["S1"]["status"] = "done"
    manifest["stories"]["S1"]["pr_url"] = "https://example.com/pr/48"
    (plan_dir / "reingest.manifest.json").write_text(json.dumps(manifest))

    result = p.ingest_plan("reingest", only_epics=["E2"])

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "reingest")
    assert merged["stories"]["S1"]["status"] == "done"
    assert merged["stories"]["S1"]["pr_url"] == "https://example.com/pr/48"
    assert "S2" in merged["stories"]


def test_ingest_plan_reingest_refreshes_authored_fields_preserves_runtime_status(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [
            _story(key="S1", agent_instructions="Build v1."),
        ]}],
    }
    (plan_dir / "refresh.json").write_text(json.dumps(plan))
    p.ingest_plan("refresh")
    manifest = _read_manifest(plan_dir, "refresh")
    manifest["stories"]["S1"]["status"] = "done"
    manifest["stories"]["S1"]["pr_url"] = "https://example.com/pr/1"
    (plan_dir / "refresh.manifest.json").write_text(json.dumps(manifest))

    plan["epics"][0]["stories"][0]["agent_instructions"] = "Build v2, with edge cases."
    (plan_dir / "refresh.json").write_text(json.dumps(plan))
    result = p.ingest_plan("refresh")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "refresh")
    assert merged["stories"]["S1"]["agent_instructions"] == "Build v2, with edge cases."
    assert merged["stories"]["S1"]["status"] == "done"
    assert merged["stories"]["S1"]["pr_url"] == "https://example.com/pr/1"


def test_ingest_plan_overwrite_true_drops_untouched_epics(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "E1", "stories": [_story(key="S1")]},
            {"summary": "E2", "stories": [_story(key="S2")]},
        ],
    }
    (plan_dir / "ovr.json").write_text(json.dumps(plan))
    p.ingest_plan("ovr")

    result = p.ingest_plan("ovr", only_epics=["E2"], overwrite=True)

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ovr")
    assert "S1" not in manifest["stories"]
    assert "S2" in manifest["stories"]


def test_ingest_plan_reingest_preserves_top_level_paused_and_fallback_fields(
    plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1")]}],
    }
    (plan_dir / "topkeys.json").write_text(json.dumps(plan))
    p.ingest_plan("topkeys")
    manifest = _read_manifest(plan_dir, "topkeys")
    manifest["paused"] = True
    manifest["local_model_fallback"] = "glm-5.2:cloud"
    (plan_dir / "topkeys.manifest.json").write_text(json.dumps(manifest))

    result = p.ingest_plan("topkeys")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "topkeys")
    assert merged["paused"] is True
    assert merged["local_model_fallback"] == "glm-5.2:cloud"


def test_ingest_plan_skips_when_lock_held(plan_dir, monkeypatch, tmp_path):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1")]}],
    }
    (plan_dir / "ilk2.json").write_text(json.dumps(plan))

    def _boom(*a, **kw):
        raise AssertionError("a locked-out ingest_plan must not touch Plane or the manifest")
    monkeypatch.setattr(pt, "plane_request", _boom)

    lock_path = plan_dir / "ilk2.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.ingest_plan("ilk2")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert not (plan_dir / "ilk2.manifest.json").exists()


# ---------- Review gate + auto-PR ----------


def test_open_pr_pushes_branch_before_creating_pr(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    url = p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    assert url == "https://gh/pr/1"
    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    pr_calls = [c for c in calls if c[:2] == ["gh", "pr"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert calls.index(push_calls[0]) < calls.index(pr_calls[0])
    assert "agent/s1" in push_calls[0]


def test_open_pr_force_pushes_with_lease_to_survive_a_prior_rebase(monkeypatch, tmp_path):
    """A story's agent/<key> branch is rebased onto origin/master before every
    resumed dispatch (see _rebase_onto_master / the "worktree base predates
    origin/master" notification path). If review_story already pushed once
    for an earlier PR (e.g. review APPROVEd, PR opened, then a later rework
    round rebased the branch again), the rebase rewrites local commit SHAs so
    they diverge from what's already on the remote. A plain (non-force) `git
    push` is then rejected as non-fast-forward every single time review_story
    retries - reproduced live 2026-08-21 on story 24cfe47f: 100+ identical
    "review APPROVEd but could not open PR (CalledProcessError)" notifications
    over 2+ hours, the story permanently stuck at tests_passed. Since this
    branch is exclusively owned by the pipeline's own dispatched agent (no
    external pusher to race), a force-with-lease push - the same pattern
    already used by _rebase_and_push_for_merge - is safe and must be used
    here too so a legitimately-rebased branch can still be pushed."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "https://gh/pr/1\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert push_calls, "expected the branch to be pushed before opening a PR"
    assert "--force-with-lease" in push_calls[0]


def test_open_pr_reuses_existing_pr_when_one_already_exists(monkeypatch, tmp_path):
    """A dispatched agent may have already run `gh pr create` itself before
    review_story gets to it. _open_pr must recover the existing PR's URL
    instead of bubbling up gh's "already exists" failure."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            raise p.subprocess.CalledProcessError(
                1, cmd,
                output="",
                stderr=(
                    'a pull request for branch "agent/s1" into branch "main" '
                    "already exists:\nhttps://github.com/org/repo/pull/4\n"
                ),
            )
        class Result:
            stdout = "https://github.com/org/repo/pull/4\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    url = p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})

    assert url == "https://github.com/org/repo/pull/4"
    view_calls = [c for c in calls if c[:2] == ["gh", "pr"] and "view" in c]
    assert view_calls, "expected a fallback `gh pr view` lookup"


def test_open_pr_reraises_other_gh_pr_create_failures(monkeypatch, tmp_path):
    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "create"]:
            raise p.subprocess.CalledProcessError(
                1, cmd, output="", stderr="some other failure",
            )
        class Result:
            stdout = ""
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    with pytest.raises(p.subprocess.CalledProcessError):
        p._open_pr(str(tmp_path), "S1", {"summary": "Add thing"})


def test_parse_verdict_variants():
    assert p._parse_verdict("...\nVERDICT: APPROVE\n") == "APPROVE"
    assert p._parse_verdict("VERDICT: REQUEST_CHANGES") == "REQUEST_CHANGES"
    assert p._parse_verdict("no verdict here") == "UNKNOWN"


def test_parse_verdict_recognizes_approve_with_fix():
    """APPROVE_WITH_FIX must parse as its own distinct verdict, not collapse
    into bare APPROVE via prefix-matching in the regex alternation (ordering
    matters: APPROVE_WITH_FIX must be tried before the bare APPROVE
    alternative, or a naive alternation matches "APPROVE" as a substring of
    "APPROVE_WITH_FIX" and silently drops the _WITH_FIX distinction)."""
    assert p._parse_verdict("...\nVERDICT: APPROVE_WITH_FIX\n") == "APPROVE_WITH_FIX"
    # Bare APPROVE must still parse as plain APPROVE, not get upgraded.
    assert p._parse_verdict("VERDICT: APPROVE") == "APPROVE"


def test_run_reviewer_prompt_asks_reviewer_to_flag_missing_documentation(
    agents_dir, monkeypatch,
):
    """The reviewer rubric must explicitly ask whether a user-visible change
    needs a documentation update, not just correctness/mutation/validation -
    otherwise a story can cleanly pass review and merge while silently
    missing the README update CLAUDE.md's Definition of Done requires
    (observed: REVIEW-LOG/MODEL-TUNING-TABLE/GPTOSS-TEMP03 merged without
    it; only REVIEW-UNKNOWN got documented, because that story's own
    agent_instructions happened to ask for it explicitly)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"]
    assert "documentation" in prompt.lower()
    assert "README" in prompt


def test_run_reviewer_prompt_does_not_block_on_docs_for_brand_new_code(
    agents_dir, monkeypatch,
):
    """The documentation check (see the test above) must not fire as a
    blocker for a brand-new addition nothing else in the repo calls yet --
    only for behavior EXISTING callers/users already depend on. Without this
    distinction, every new-module story (the common case for early-stage
    work) burns a full extra dispatch+rework+re-review cycle on a doc nit
    CLAUDE.md's own Blocking-vs-Suggestion guidance says should default to
    Suggestion, not REQUEST_CHANGES -- and each of those cycles is a full
    reviewer invocation, cloud or local, that a spurious block doesn't need
    to spend (observed: a throwaway benchmark task's rate limiter got
    REQUEST_CHANGES purely for missing README docs on a brand-new, not-yet-
    consumed class, 2026-07-04)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"].lower()
    assert "existing caller" in prompt or "existing consumer" in prompt or "already depend" in prompt
    assert "suggestion" in prompt
    assert "brand-new" in prompt or "brand new" in prompt


def test_run_reviewer_prompt_asks_for_every_blocking_finding_in_one_pass(
    agents_dir, monkeypatch,
):
    """The reviewer rubric must ask for ALL Blocking findings in a single
    review, not just the first one noticed - otherwise a weak local
    implementer burns a full rework cycle per finding, and each cycle is a
    fresh opportunity to regress already-correct code (observed live
    2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD: review #1 flagged only the
    missing docstring; review #2, on otherwise-correct code, surfaced a
    SECOND pre-existing issue (validate-before-lock) that was visible in
    review #1's diff but never raised there; the extra rework cycle this
    forced is where the implementation broke)."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"].lower()
    assert "every" in prompt and "blocking" in prompt
    assert "rework" in prompt


def test_run_reviewer_prompt_omits_approve_with_fix_by_default(agents_dir, monkeypatch):
    """The reviewer self-fix option (APPROVE_WITH_FIX) must be off by
    default (secure-by-default: this is a new capability that auto-commits
    reviewer-authored code) - an operator opts in explicitly via
    PIPELINE_REVIEWER_AUTO_FIX. Without the env var set, the prompt must
    never mention the option, even for a low-risk story."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.delenv("PIPELINE_REVIEWER_AUTO_FIX", raising=False)

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="low")

    assert "APPROVE_WITH_FIX" not in captured["prompt"]


def test_run_reviewer_prompt_mentions_approve_with_fix_when_enabled_and_low_risk(
    agents_dir, monkeypatch,
):
    """Enabled + low risk is the only combination that offers the reviewer
    the self-fix option."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.setenv("PIPELINE_REVIEWER_AUTO_FIX", "1")

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="low")

    assert "APPROVE_WITH_FIX" in captured["prompt"]


def test_run_reviewer_prompt_omits_approve_with_fix_for_high_risk_even_when_enabled(
    agents_dir, monkeypatch,
):
    """Defense in depth at the prompt-construction layer, not just the
    harness-side guardrail: a high-risk story never even gets offered the
    self-fix option, regardless of the operator's global opt-in."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    monkeypatch.setenv("PIPELINE_REVIEWER_AUTO_FIX", "1")

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", risk="high")

    assert "APPROVE_WITH_FIX" not in captured["prompt"]


def test_run_reviewer_does_not_run_test_suite(agents_dir, tmp_path, monkeypatch):
    """Real-world PR review: the reviewer reviews the diff and trusts CI.
    review_story only runs when status == tests_passed, so the suite is
    already green; a reviewer-driven rerun is pure duplicate spend (a full
    agentic Bash tool-loop re-executing what check_story_status just ran).
    The prompt must NOT instruct the reviewer to run the test suite, and
    must not inject any resolved test command."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer(str(tmp_path), "agent/some-branch")

    prompt = captured["prompt"]
    assert "Run the test suite" not in prompt
    assert "do not substitute" not in prompt.lower()
    assert "-m pytest" not in prompt


def test_run_reviewer_first_review_covers_full_branch_diff(agents_dir, tmp_path, monkeypatch):
    """First review (no prior REQUEST_CHANGES, since_sha unset): review the
    full branch diff -- no incremental scoping, no test-suite rerun."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer(str(tmp_path), "agent/some-branch")

    prompt = captured["prompt"]
    assert "agent/some-branch" in prompt
    assert "Run the test suite" not in prompt
    # No incremental since-SHA scoping on a first review.
    assert "..HEAD" not in prompt


def test_run_reviewer_uses_review_model_override_when_backend_is_local(agents_dir, monkeypatch):
    """Asymmetric review: both software-engineer.md and code-reviewer.md
    declare `model: sonnet`, so without an override dispatch and review
    resolve to the identical concrete local model - a model reviewing its
    own work with identical weights. PIPELINE_LOCAL_REVIEW_MODEL lets
    review run on a different model, but only when the review backend is
    actually local (passing a bare Ollama tag like "devstral:24b" as the
    Claude CLI's --model would break cloud review)."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["model"] == "devstral:24b"


def test_run_reviewer_ignores_review_model_override_when_backend_is_claude(agents_dir, monkeypatch):
    """Regression guard: PIPELINE_LOCAL_REVIEW_MODEL must NOT leak into a
    cloud (claude) review - it must keep using the persona's declared tier
    ("sonnet") so ClaudeCliDriver gets a real Claude model name, not an
    Ollama tag."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "claude")
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured["model"] == "sonnet"


def test_run_reviewer_explicit_local_backend_name_honors_review_model_override(agents_dir, monkeypatch):
    """review_story's FM-B rate-limit fallback calls _run_reviewer with an
    explicit backend_name="local" override (not via the env var) - the
    review-model override must apply in that path too."""
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", backend_name="local")

    assert captured["model"] == "devstral:24b"


def test_run_reviewer_explicit_provider_name_honors_review_model_override(agents_dir, monkeypatch):
    """T16: an explicitly-pinned provider name (not just the "local" alias)
    must also count as local-family for the review-model override gate."""
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", backend_name="lmstudio")

    assert captured["model"] == "devstral:24b"


