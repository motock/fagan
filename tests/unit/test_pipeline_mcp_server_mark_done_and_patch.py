"""Tests for the pipeline MCP server: mark_story_done plan-completion signal and retro tracking, and patch_story/set_story_status.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

from app import (
    role_registry,
)
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
)


# ---------- review consults role_registry (provider + model fallback) ----------
def test_run_reviewer_provider_from_registry_when_env_and_backend_name_unset(
    agents_dir, monkeypatch,
):
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_REVIEW_MODEL", raising=False)
    registry = {
        "providers": {"mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}}},
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    calls = []

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            calls.append(model)
            return "VERDICT: APPROVE"

    captured_name = {}

    def _fake_get_backend(role, name=None):
        captured_name["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert captured_name["name"] == "mlx"
    assert calls == ["mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"]


def test_run_reviewer_plan_role_config_beats_registry(agents_dir, monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_REVIEW_MODEL", raising=False)
    registry = {
        "providers": {
            "mlx": {"models": {"qwen": {"tag": "mlx-tag"}}},
            "claude": {"models": {}},
        },
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    captured_name = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            return "VERDICT: APPROVE"

    def _fake_get_backend(role, name=None):
        captured_name["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._run_reviewer(
        "/tmp/some-worktree", "agent/some-branch",
        plan_role_config={"review": {"provider": "claude"}},
    )

    assert captured_name["name"] == "claude"


def test_run_reviewer_local_review_model_override_still_beats_registry(
    agents_dir, monkeypatch,
):
    """PIPELINE_LOCAL_REVIEW_MODEL must remain the top-priority override for
    local-family reviews, even when the registry also configures a model for
    the resolved provider."""
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MODEL", "devstral:24b")
    registry = {
        "providers": {"mlx": {"models": {"qwen": {"tag": "mlx-tag"}}}},
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    calls = []

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            calls.append(model)
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    assert calls == ["devstral:24b"]


def test_run_reviewer_incremental_review_scopes_to_since_sha(agents_dir, monkeypatch):
    """Rework review (since_sha set to the commit the last REQUEST_CHANGES was
    raised against): the reviewer reviews ONLY the new commits pushed since,
    not the whole branch from zero -- mirroring a real PR re-review where the
    developer pushed changes, CI went green, and the reviewer reviews just
    the new diff. Unchanged, already-approved files are not re-reviewed."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch", since_sha="abc123def")

    prompt = captured["prompt"]
    assert "git diff abc123def..HEAD" in prompt
    assert "already approved" in prompt.lower() or "not changed since" in prompt.lower()
    assert "Run the test suite" not in prompt
    # Substantive review criteria are still present, unchanged.
    for num in ["(1)", "(2)", "(3)", "(4)"]:
        assert num in prompt


def test_run_reviewer_does_not_inject_resolved_test_command(agents_dir, monkeypatch, tmp_path):
    """The reviewer no longer runs the suite, so the detected test command
    (Python venv pytest, npm, make, ...) is never injected into the prompt.
    Guards the venv-pytest path that previously burned a reviewer's whole
    step budget, and the multi-language fallback path, are both gone."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")

    prompt = captured["prompt"]
    assert "-m pytest" not in prompt
    assert "npm test" not in prompt
    assert ".venv" not in prompt
    assert "do not substitute" not in prompt.lower()


def test_run_reviewer_does_not_crash_when_detection_unavailable(
    agents_dir, monkeypatch,
):
    """A worktree path that doesn't exist (or has no build marker) must not
    crash _run_reviewer or block review -- with the suite no longer run by
    the reviewer, there is nothing to detect, so review proceeds on the
    diff alone."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    output = p._run_reviewer("/tmp/does-not-exist", "agent/some-branch")

    assert output == "VERDICT: APPROVE"
    prompt = captured["prompt"]
    assert "Run the test suite" not in prompt
    assert "do not substitute" not in prompt.lower()


def test_run_reviewer_proceeds_on_worktree_with_no_build_marker(
    agents_dir, monkeypatch, tmp_path,
):
    """Negative/boundary case: a worktree with no recognizable build marker
    at all still reviews cleanly -- the reviewer reviews the diff, not the
    test suite, so there is nothing to detect and no fallback command to
    inject."""
    empty_worktree = tmp_path / "empty-worktree"
    empty_worktree.mkdir()
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())

    p._run_reviewer(str(empty_worktree), "agent/some-branch")

    prompt = captured["prompt"]
    assert "Run the test suite" not in prompt
    assert "npm test" not in prompt
    assert "agent/some-branch" in prompt


def test_review_story_approve_opens_pr(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "rv", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    result = p.review_story("rv", "S1")
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert result["pr_url"] == "https://gh/pr/1"
    story = _read_manifest(plan_dir, "rv")["stories"]["S1"]
    assert story["status"] == "pr_open"
    assert story["pr_url"] == "https://gh/pr/1"


def test_review_story_skips_llm_reviewer_on_known_failing_acceptance_review(
    plan_dir, agents_dir, monkeypatch,
):
    """Mode 40: a story routed to review via acceptance_failed_review
    (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1) with a recorded failing
    last_test_check must skip the expensive LLM reviewer call entirely and
    synthesize REQUEST_CHANGES feedback directly from the test output -
    the LLM reviewer can't meaningfully correctness-review a submission
    that doesn't pass its own tests, and a live incident showed the
    reviewer's own principal finding was just restating this same
    failing-test list."""
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: reviewer_calls.append(1) or "VERDICT: APPROVE")
    _write_manifest(plan_dir, "rv2", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance_failed_review": True,
               "last_test_check": {
                   "cmd": ["pytest", "-q"], "returncode": 1,
                   "stdout_tail": "FAILED test_foo.py::test_bar - assert False\n",
                   "stderr_tail": "",
               }},
    })

    result = p.review_story("rv2", "S1")

    assert reviewer_calls == []  # LLM reviewer never invoked
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    story = _read_manifest(plan_dir, "rv2")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert "FAILED test_foo.py::test_bar" in story["review_feedback"]
    assert story["last_review_findings"] == ["test_foo.py"]
    assert story["rework_attempts"] == 1


def test_review_story_skips_llm_reviewer_only_when_last_test_check_sha_matches_head(
    plan_dir, agents_dir, monkeypatch,
):
    """A last_test_check recorded at a PAST commit (stale sha) must NOT be
    trusted by the skip_llm_reviewer fast path - the worktree HEAD has since
    moved, so the recorded failure may no longer exist. Only when the recorded
    sha matches the current HEAD should the fast path fire."""
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: reviewer_calls.append(1) or "VERDICT: APPROVE")

    # The worktree must exist on disk so review_story computes before_sha
    # from it (via the monkeypatched git rev-parse below).
    (plan_dir / "wt").mkdir(parents=True, exist_ok=True)

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == "git" and cmd[1] == "rev-parse":
            return subprocess.CompletedProcess(cmd, 0, stdout="bbb222\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    # Stale: recorded failure at commit aaa111, but HEAD is now bbb222.
    _write_manifest(plan_dir, "rvstale", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance_failed_review": True,
               "last_test_check": {
                   "cmd": ["pytest", "-q"], "returncode": 1,
                   "sha": "aaa111",
                   "stdout_tail": "FAILED test_foo.py::test_bar - assert False\n",
                   "stderr_tail": "",
               }},
    })
    result = p.review_story("rvstale", "S1")
    assert reviewer_calls == [1], (
        "stale last_test_check (sha aaa111 != HEAD bbb222) must fall through "
        "to the real reviewer, not take the skip fast path"
    )

    # Fresh: recorded failure at the current HEAD bbb222 -> fast path fires.
    reviewer_calls.clear()
    _write_manifest(plan_dir, "rvfresh", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance_failed_review": True,
               "last_test_check": {
                   "cmd": ["pytest", "-q"], "returncode": 1,
                   "sha": "bbb222",
                   "stdout_tail": "FAILED test_foo.py::test_bar - assert False\n",
                   "stderr_tail": "",
               }},
    })
    result = p.review_story("rvfresh", "S1")
    assert reviewer_calls == []
    assert result["verdict"] == "REQUEST_CHANGES"
    story = _read_manifest(plan_dir, "rvfresh")["stories"]["S1"]
    assert "FAILED test_foo.py::test_bar" in story["review_feedback"]


def test_review_story_calls_llm_reviewer_normally_without_acceptance_failed_review(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression bar: an ordinary tests_passed story (acceptance_failed_review
    not set) always goes through the real reviewer, unaffected by this
    story's field."""
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: reviewer_calls.append(1) or "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    _write_manifest(plan_dir, "rv3", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    result = p.review_story("rv3", "S1")

    assert reviewer_calls == [1]
    assert result["verdict"] == "APPROVE"


def test_review_story_calls_llm_reviewer_when_acceptance_failed_review_but_no_last_test_check(
    plan_dir, agents_dir, monkeypatch,
):
    """Defensive fallback: acceptance_failed_review=True but no
    last_test_check recorded (shouldn't normally happen, but must not
    crash) falls back to the real reviewer rather than synthesizing
    feedback from nothing."""
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: reviewer_calls.append(1) or "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    _write_manifest(plan_dir, "rv4", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance_failed_review": True},
    })

    result = p.review_story("rv4", "S1")

    assert reviewer_calls == [1]
    assert result["verdict"] == "APPROVE"


def test_review_story_calls_llm_reviewer_when_last_test_check_passed(
    plan_dir, agents_dir, monkeypatch,
):
    """acceptance_failed_review=True but last_test_check.returncode == 0
    (the acceptance oracle failed while the detected test command itself
    passed - a real, distinct case) must still call the real reviewer,
    since there's no test failure to synthesize feedback from."""
    reviewer_calls = []
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: reviewer_calls.append(1) or "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    _write_manifest(plan_dir, "rv5", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance_failed_review": True,
               "last_test_check": {"cmd": ["pytest"], "returncode": 0,
                                    "stdout_tail": "", "stderr_tail": ""}},
    })

    result = p.review_story("rv5", "S1")

    assert reviewer_calls == [1]
    assert result["verdict"] == "APPROVE"


def test_review_story_passes_plan_role_config_from_manifest_to_reviewer(
    plan_dir, agents_dir, monkeypatch,
):
    """End-to-end: a plan's manifest role_config block must actually reach
    _run_reviewer's plan_role_config kwarg - not just be tolerated by
    signature, but genuinely read from the plan on disk and threaded
    through review_story."""
    (plan_dir / "rvcfg.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {"summary": "Add thing", "status": "tests_passed",
                   "worktree": str(plan_dir / "wt"), "risk": "low"},
        },
        "role_config": {"review": {"provider": "ollama"}},
    }))
    captured = {}

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None, since_sha=None, risk=None):
        captured["plan_role_config"] = plan_role_config
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("rvcfg", "S1")

    assert captured["plan_role_config"] == {"review": {"provider": "ollama"}}


def test_review_story_request_changes_opens_no_pr(plan_dir, agents_dir, monkeypatch):
    # T11 (2026-07-12): updated. This test's reviewer stub is a bare
    # "VERDICT: REQUEST_CHANGES" with no findings text - previously asserted
    # as a genuine rejection (changes_requested, rework_attempts consumed),
    # but that was exactly the bug T11 fixes: an empty REQUEST_CHANGES gives
    # a redispatched agent nothing to act on and was silently burning rework
    # budget. It now takes the inconclusive path (status unchanged, no PR,
    # no rework_attempts) - see test_review_story_bare_request_changes_is_treated_as_inconclusive
    # for the dedicated coverage of that path and
    # test_review_story_genuine_request_changes_still_increments_rework for
    # the regression guard confirming real findings text still counts.
    _write_manifest(plan_dir, "rv", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: REQUEST_CHANGES")

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("rv", "S1")
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "tests_passed"
    assert result.get("pr_url") is None
    story = _read_manifest(plan_dir, "rv")["stories"]["S1"]
    assert "pr_url" not in story
    assert "rework_attempts" not in story
    assert story["review_inconclusive_count"] == 1


def test_review_story_persists_feedback_on_request_changes(plan_dir, agents_dir, monkeypatch):
    # The reviewer's reasoning must be stored, not just the verdict, so a
    # redispatched agent knows what to fix.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvfb", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    reviewer_output = ("The error path is untested and the SQL is injectable.\n"
                       "VERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)

    p.review_story("rvfb", "S1")

    story = _read_manifest(plan_dir, "rvfb")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["review_feedback"] == reviewer_output
    assert story["rework_attempts"] == 1


def test_review_story_request_changes_warns_rework_when_acceptance_oracle_currently_passes(
    plan_dir, agents_dir, monkeypatch,
):
    """Mode 20 (2026-07-17, verified by replay): when a story carries an
    acceptance block and the oracle currently PASSES against the worktree but
    the reviewer still returned REQUEST_CHANGES (e.g. it flagged something
    outside the oracle's scope, such as a bug in the agent's OWN test file),
    the rework feedback must say so explicitly. Without this, a redispatched
    agent has no signal that a whole-file rewrite risks regressing already-
    correct, oracle-green behavior - observed: this exact gap let a rework
    destroy a passing backward-jump fix (token_bucket/mlx,
    role_registry_prod_verify5_20260717_073857)."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 3)
    _write_manifest(plan_dir, "rvoracle", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance": [{"path": "test_acceptance.py"}]},
    })
    reviewer_output = "Some unrelated nit.\nVERDICT: REQUEST_CHANGES"
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})

    p.review_story("rvoracle", "S1")

    story = _read_manifest(plan_dir, "rvoracle")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert "acceptance oracle is currently passing" in story["review_feedback"].lower()
    assert reviewer_output in story["review_feedback"]


def test_review_story_request_changes_no_oracle_warning_when_oracle_fails(
    plan_dir, agents_dir, monkeypatch,
):
    """When the acceptance oracle is ALSO failing, no false reassurance
    should be injected - the feedback stays exactly the reviewer's own
    text, since there's nothing green to protect from regression."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 3)
    _write_manifest(plan_dir, "rvoraclefail", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance": [{"path": "test_acceptance.py"}]},
    })
    reviewer_output = "Real bug found.\nVERDICT: REQUEST_CHANGES"
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "boom"})

    p.review_story("rvoraclefail", "S1")

    story = _read_manifest(plan_dir, "rvoraclefail")["stories"]["S1"]
    assert story["review_feedback"] == reviewer_output


def test_review_story_request_changes_no_oracle_check_without_acceptance_block(
    plan_dir, agents_dir, monkeypatch,
):
    """Stories without an acceptance block (the common case) are unaffected -
    no oracle re-verification call, feedback unchanged from before Mode 20's
    fix."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvnoacc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    reviewer_output = "Real bug found.\nVERDICT: REQUEST_CHANGES"
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)
    calls = []
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: calls.append(1) or {"state": "pass", "error": ""})

    p.review_story("rvnoacc", "S1")

    story = _read_manifest(plan_dir, "rvnoacc")["stories"]["S1"]
    assert story["review_feedback"] == reviewer_output
    assert calls == []


# ---------- fresh-rework-on-regression (2026-07-29) ----------
# The 2026-07-29 gpt-oss E2E finding (bcca562e/token_report): a rework
# redispatch that RESUMES the prior dispatch's transcript replays whatever
# churn led to a regression, compounding it (500s + acceptance 11/11 -> 9).
# The SAME story recovered cleanly on a FRESH rework (transcript deleted,
# from-scratch prompt) once the poisoned transcript was removed. The oracle
# re-verify above already tells review_story whether this cycle's rework
# just broke previously-passing behavior - when it did, delete the
# transcript so the NEXT redispatch (resume_via_transcript in dispatch_story)
# can't resume it and is forced onto the from-scratch rework prompt instead.

def test_review_story_leaves_transcript_on_first_review_with_failing_oracle(
    plan_dir, agents_dir, monkeypatch,
):
    """A failing oracle is NOT by itself a regression. With
    PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1 (set in this install's env) the most
    common way a story reaches review with a red oracle is a FIRST dispatch
    that was simply incomplete - no prior passing state to have regressed
    from. Deleting the transcript there discards the richest context a rework
    could resume from, to fix a problem that never happened. Only a story
    that has already been through at least one rework cycle
    (rework_attempts > 0, which at this point in the cycle holds the count
    BEFORE this one is added) has a prior state it could have regressed."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    transcript_path = worktree / ".agent_transcript.json"
    transcript_path.write_text('[{"role": "system", "content": "x"}]')
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 3)
    _write_manifest(plan_dir, "rvfirstfail", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(worktree), "risk": "low",
               "acceptance": [{"path": "test_acceptance.py"}]},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "Incomplete.\nVERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "boom"})

    p.review_story("rvfirstfail", "S1")

    assert transcript_path.exists(), (
        "a first review with a failing oracle is an incomplete attempt, not a "
        "regression - the transcript must survive for the rework to resume"
    )


def test_review_story_deletes_transcript_when_oracle_regresses(
    plan_dir, agents_dir, monkeypatch,
):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    transcript_path = worktree / ".agent_transcript.json"
    transcript_path.write_text('[{"role": "system", "content": "x"}]')
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 3)
    _write_manifest(plan_dir, "rvregress", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(worktree), "risk": "low",
               # A rework has already happened, so a now-failing oracle means
               # this cycle's dispatch broke previously-working behavior.
               "rework_attempts": 1,
               "acceptance": [{"path": "test_acceptance.py"}]},
    })
    reviewer_output = "Real bug found.\nVERDICT: REQUEST_CHANGES"
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "boom"})

    p.review_story("rvregress", "S1")

    assert not transcript_path.exists(), (
        "transcript must be deleted so the next redispatch can't resume "
        "the churn that caused the regression"
    )
    notif = (plan_dir / "rvregress.notifications.log").read_text()
    assert "regressed" in notif.lower()
    assert "fresh" in notif.lower()


def test_review_story_leaves_transcript_when_oracle_still_passing(
    plan_dir, agents_dir, monkeypatch,
):
    """The transcript deletion is specifically a regression response - a
    REQUEST_CHANGES with the oracle still passing (e.g. a finding outside
    its scope) must not discard useful resumable context."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    transcript_path = worktree / ".agent_transcript.json"
    transcript_path.write_text('[{"role": "system", "content": "x"}]')
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 3)
    _write_manifest(plan_dir, "rvnoregress", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(worktree), "risk": "low",
               "acceptance": [{"path": "test_acceptance.py"}]},
    })
    reviewer_output = "Some unrelated nit.\nVERDICT: REQUEST_CHANGES"
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: reviewer_output)
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})

    p.review_story("rvnoregress", "S1")

    assert transcript_path.exists()


def test_review_story_clears_feedback_and_rework_on_approve(plan_dir, agents_dir, monkeypatch):
    # An approval after prior rework cycles must wipe the stale feedback/counter
    # so the story records a clean approval.
    _write_manifest(plan_dir, "rvclear", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "review_feedback": "old gripes", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("rvclear", "S1")

    story = _read_manifest(plan_dir, "rvclear")["stories"]["S1"]
    assert story["status"] == "pr_open"
    assert "review_feedback" not in story
    assert "rework_attempts" not in story


def test_review_story_parks_after_rework_budget_exhausted(plan_dir, agents_dir, monkeypatch):
    # A story the reviewer keeps rejecting must eventually park for human review
    # rather than looping through redispatch forever.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvpark", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "still bad\nVERDICT: REQUEST_CHANGES")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.review_story("rvpark", "S1")

    story = _read_manifest(plan_dir, "rvpark")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 3
    assert result["status"] == "parked"
    assert len(notes) == 1 and "S1" in notes[0]


def test_review_story_oracle_backed_story_parks_after_lower_rework_cap(
    plan_dir, agents_dir, monkeypatch,
):
    """A story with an acceptance oracle already has an objective,
    pre-verified correctness signal (it reached review because tests -
    including the oracle - passed). Burning the full REWORK_MAX_ATTEMPTS
    budget chasing a reviewer's beyond-oracle findings on already-correct
    code just wastes cycles before it parks anyway; PIPELINE_REWORK_MAX_
    ATTEMPTS_ORACLE (default 1) converges faster for these stories."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 1)
    _write_manifest(plan_dir, "rvoracle", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "edge case missing\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvoracle", "S1")

    story = _read_manifest(plan_dir, "rvoracle")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 1
    assert result["status"] == "parked"


def test_review_story_non_oracle_story_still_uses_full_rework_budget(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression guard: a story with NO acceptance oracle (ordinary TDD -
    the agent's own tests are the only correctness signal, review judgment
    matters more) must keep using the full REWORK_MAX_ATTEMPTS, unaffected
    by the oracle-backed cap - one REQUEST_CHANGES here must NOT park."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 1)
    _write_manifest(plan_dir, "rvnoacc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "needs work\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvnoacc", "S1")

    story = _read_manifest(plan_dir, "rvnoacc")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 1
    assert result["status"] == "changes_requested"


def test_review_story_oracle_backed_empty_acceptance_list_uses_full_budget(
    plan_dir, agents_dir, monkeypatch,
):
    """Boundary: a story carrying acceptance=[] (present but empty) is not
    actually oracle-backed - falsy, same as no acceptance at all - so it
    must use the full rework budget, not the oracle cap."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 1)
    _write_manifest(plan_dir, "rvempty", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "acceptance": []},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "needs work\nVERDICT: REQUEST_CHANGES")

    p.review_story("rvempty", "S1")

    story = _read_manifest(plan_dir, "rvempty")["stories"]["S1"]
    assert story["status"] == "changes_requested"


def test_review_story_escalated_oracle_story_gets_escalated_cap_not_oracle_cap(
    plan_dir, agents_dir, monkeypatch,
):
    """2026-07-04's auto-escalation benchmark validation: 6 of 11 escalated
    cells parked after exactly 1 post-escalation rework cycle, because the
    oracle cap (built to converge LOCAL review fast) still applied to
    Claude's shot at the same feedback. Once story["escalated"] is True,
    REWORK_MAX_ATTEMPTS_ESCALATED must govern instead - regardless of
    whether the story also carries an acceptance oracle."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 1)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ESCALATED", 3)
    _write_manifest(plan_dir, "escoracle", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True,
               "acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}],
               "rework_attempts": 0},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("escoracle", "S1")

    story = _read_manifest(plan_dir, "escoracle")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 1
    assert result["status"] == "changes_requested"


def test_review_story_escalated_oracle_story_parks_after_escalated_cap_exhausted(
    plan_dir, agents_dir, monkeypatch,
):
    """Boundary: the escalated cap is still finite - once IT is exhausted,
    the story must park for real (no further fallback past Claude)."""
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ORACLE", 1)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_ESCALATED", 3)
    _write_manifest(plan_dir, "escoracledone", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True,
               "acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}],
               "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("escoracledone", "S1")

    story = _read_manifest(plan_dir, "escoracledone")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["rework_attempts"] == 3
    assert result["status"] == "parked"


def test_review_story_survives_unexpected_reviewer_exception(plan_dir, agents_dir, monkeypatch):
    """A local reviewer's internal error (e.g. a malformed backend response
    surfacing as a bare KeyError) must not crash review_story - it must
    resolve to the same UNKNOWN-verdict inconclusive-retry path a genuinely
    inconclusive review already takes (fail-safe), not be silently treated
    as an APPROVE (fail-closed), must not burn the rework budget (updated for
    REVIEW-UNKNOWN: a non-rate-limited UNKNOWN no longer counts as
    REQUEST_CHANGES), and must notify the user for observability without
    leaking raw exception text."""
    _write_manifest(plan_dir, "rvcrash", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    def _boom(wt, br, **k):
        raise KeyError("message")

    monkeypatch.setattr(p, "_run_reviewer", _boom)
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.review_story("rvcrash", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "UNKNOWN"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "rvcrash")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["review_inconclusive_count"] == 1
    assert "rework_attempts" not in story
    assert "review_feedback" not in story
    assert any("KeyError" in n for n in notes)
    assert not any("message" in n for n in notes)


def test_merge_pr_does_not_pass_delete_branch_to_gh(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = "merged\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    result = p._merge_pr(str(tmp_path / "wt"), "S1")

    assert result == "merged"
    gh_calls = [c for c in calls if c[:2] == ["gh", "pr"]]
    assert "--delete-branch" not in gh_calls[0]


def test_merge_pr_cleans_up_worktree_and_branches_after_merge(monkeypatch, tmp_path):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        class Result:
            stdout = "merged\n"
            returncode = 0
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)
    worktree = str(tmp_path / "wt")
    p._merge_pr(worktree, "S1")

    cmds = [c for c, _cwd in calls]
    assert ["git", "worktree", "remove", "--force", worktree] in cmds
    assert ["git", "branch", "-D", "agent/s1"] in cmds
    assert ["git", "push", "origin", "--delete", "agent/s1"] in cmds
    # Cleanup must run from REPO_ROOT, not the worktree being removed.
    cleanup_cwds = [cwd for cmd, cwd in calls if cmd[:2] == ["git", "worktree"]]
    assert all(cwd == tmp_path for cwd in cleanup_cwds)


def test_commit_wip_does_not_track_real_agent_log_file(tmp_path):
    p.subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 1;\n")
    p.subprocess.run(["git", "add", "feature.ts"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 2;\n")
    (tmp_path / "agent.log").write_text("session narration, not project code\n")

    p._commit_wip(str(tmp_path), "S1", "interrupted")

    tracked = p.subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert "agent.log" not in tracked
    assert "feature.ts" in tracked


def test_commit_wip_checkpoints_when_agent_log_is_git_ignored(tmp_path):
    """Regression: a worktree may have agent.log locally git-ignored (via
    .git/info/exclude or .gitignore). The old `git add -A -- . :!agent.log`
    named agent.log in the pathspec, so git rejected the whole add ("paths are
    ignored... use -f", exit 1) and _commit_wip raised before committing - the
    checkpoint was lost even though real work was staged. The commit must
    succeed and exclude agent.log regardless of whether it is git-ignored."""
    p.subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "feature.ts").write_text("export const x = 1;\n")
    p.subprocess.run(["git", "add", "feature.ts"], cwd=tmp_path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # Locally git-ignore agent.log, as a reviewer might to keep it out of diffs.
    (tmp_path / ".git" / "info" / "exclude").write_text("agent.log\n")
    (tmp_path / "feature.ts").write_text("export const x = 2;\n")
    (tmp_path / "agent.log").write_text("session narration, not project code\n")

    sha = p._commit_wip(str(tmp_path), "S1", "interrupted")

    assert sha  # a real commit sha, not a raised RuntimeError
    committed = p.subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert "feature.ts" in committed
    assert "agent.log" not in committed


