"""Tests for the pipeline MCP server: reset_false_positive_tests_passed.py unit tests, step-cap exit routing, and the infra-failure streak.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""

from app import backend
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _RATE_LIMIT_MSG,
    _clear_caches,
    _FakeProc,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)


# ---------- Gap 7: surface multi-model concurrent-dispatch risk ----------
def test_dispatch_warns_on_loaded_model_mismatch(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When MAX_CONCURRENT_AGENTS > 1, an agent is already in progress, and
    the target dispatch model is NOT the one currently loaded in Ollama,
    dispatch_story must log a WARN (via _notify_user) about a likely VRAM
    swap. Same-model dispatch and the MAX_CONCURRENT_AGENTS=1 case must
    not warn."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    # Simulate one agent already running (this is what triggers the check).
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    # And a different model is currently in VRAM.
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "swap_warn", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("swap_warn", "S1")

    # Filter for our specific warning - other notifications (e.g. Plane
    # sync failures) can land in the same list and we don't want to
    # confuse the assertion.
    swap_notes = [n for n in notes if "multi-model concurrent dispatch" in n]
    assert swap_notes, f"expected VRAM-swap warning, got: {notes}"
    assert any("devstral:24b" in n for n in swap_notes)


def test_dispatch_warns_on_loaded_model_mismatch_under_explicit_provider_name(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T16: an explicitly-pinned provider name (e.g. "lmstudio"), not just
    the "local" alias, must also count as local-family for the VRAM-swap
    concurrent-dispatch warning gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "swap_warn_lmstudio", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1235))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("swap_warn_lmstudio", "S1")

    swap_notes = [n for n in notes if "multi-model concurrent dispatch" in n]
    assert swap_notes, f"expected VRAM-swap warning, got: {notes}"


def test_dispatch_no_warn_when_same_model_loaded(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The target model is already loaded in Ollama - no swap risk, no warn."""
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models",
                        lambda ep: {"gpt-oss:20b"})

    _write_manifest(plan_dir, "same_model", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("same_model", "S1")

    assert not any("multi-model concurrent dispatch" in n for n in notes), \
        f"unexpected VRAM-swap warning: {notes}"


def test_dispatch_no_warn_when_no_agents_in_progress(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """First dispatch of a fresh plan has no concurrent agents - the
    `something_loaded != target` check is only meaningful when there's
    actually a concurrent agent that could be swapped. With zero
    in-progress, dispatch can simply load the target model and there's
    no swap risk to warn about."""
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 0)
    monkeypatch.setattr(backend, "_ollama_loaded_models",
                        lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "fresh", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("fresh", "S1")

    assert not any("multi-model concurrent dispatch" in n for n in notes)


def test_dispatch_no_warn_when_max_concurrent_agents_is_one(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When MAX_CONCURRENT_AGENTS=1 there's no concurrency window, so the
    whole class of multi-model swap risk is impossible and the warning
    should be suppressed. (Same-model dispatch in this mode is also safe
    but the bigger point is: with one slot, no second dispatch can race.)"""
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 1)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 0)
    monkeypatch.setattr(backend, "_ollama_loaded_models",
                        lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "single_slot", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("single_slot", "S1")

    assert not any("multi-model concurrent dispatch" in n for n in notes)


def test_dispatch_no_warn_for_claude_backend(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The warning is local-Ollama-specific (Claude runs in a separate
    infra). A dispatch against the Claude backend must never trigger it,
    even with MAX_CONCURRENT_AGENTS=2 and a 'loaded' Ollama model."""
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models",
                        lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "claude", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "sonnet", "backend": "claude"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("claude", "S1")

    assert not any("multi-model concurrent dispatch" in n for n in notes)


def test_dispatch_no_warn_when_resolved_tier_matches_loaded(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T-false-positive-fix: the story's declared model is a *tier* name
    ("sonnet"), not a concrete Ollama tag. The unresolved tier will never
    match anything in `loaded` (a set of concrete tags), which is exactly
    the false-positive this warning must not produce. Once the tier is
    resolved through backend._resolve_local_model to the concrete tag that
    is actually already loaded, there is no swap risk and no warning
    should fire."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "gpt-oss:20b")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    # The concrete tag the tier resolves to is already loaded.
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"gpt-oss:20b"})

    _write_manifest(plan_dir, "tier_resolves_to_loaded", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "sonnet"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1236))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("tier_resolves_to_loaded", "S1")

    assert not any("multi-model concurrent dispatch" in n for n in notes), \
        f"unexpected VRAM-swap warning (false positive on unresolved tier): {notes}"


def test_dispatch_warns_with_resolved_tag_when_tier_mismatches_loaded(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T-true-positive-preserved: the story's declared model is a tier name
    ("sonnet") that resolves to a concrete tag NOT currently loaded. The
    warning must still fire, and its message must contain the resolved
    concrete tag ("gpt-oss:20b"), not the raw tier name ("sonnet")."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "gpt-oss:20b")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"devstral:24b"})

    _write_manifest(plan_dir, "tier_resolves_to_mismatch", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "sonnet"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1237))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("tier_resolves_to_mismatch", "S1")

    swap_notes = [n for n in notes if "multi-model concurrent dispatch" in n]
    assert swap_notes, f"expected VRAM-swap warning, got: {notes}"
    assert any("gpt-oss:20b" in n for n in swap_notes)
    assert not any("sonnet" in n for n in swap_notes), \
        f"warning message must use the resolved concrete tag, not the raw tier name: {swap_notes}"


# ---------- Mode 2: detect Ollama serving parallelism at dispatch time ----------
def test_dispatch_warns_when_serving_parallelism_below_max_concurrent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Mode 2 regression guard: when a concurrent local dispatch is about
    to start and the running llama-server's -np (1) is below
    MAX_CONCURRENT_AGENTS (2), dispatch_story must warn loudly - the second
    agent will queue behind the first and hit the 180s read-silence
    timeout. This is the exact signature of an Ollama.app upgrade having
    silently dropped OLLAMA_NUM_PARALLEL back to 1 (observed 2026-07-25,
    v0.32.4). Same-model loaded so the multi-model check stays silent and
    the parallelism warning is isolated."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    # Same model loaded -> multi-model check must not fire.
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"gpt-oss:20b"})
    # Runner serving parallelism dropped to 1 by the upgrade.
    monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: 1)

    _write_manifest(plan_dir, "np_dropped", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1240))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("np_dropped", "S1")

    np_notes = [n for n in notes if "ollama serving parallelism" in n]
    assert np_notes, f"expected serving-parallelism warning, got: {notes}"
    assert any("MAX_CONCURRENT_AGENTS (2)" in n for n in np_notes), \
        f"warning must name the configured concurrency: {np_notes}"
    assert any("launchctl setenv OLLAMA_NUM_PARALLEL" in n for n in np_notes), \
        f"warning must tell the operator how to restore parallelism: {np_notes}"


def test_dispatch_no_warn_when_serving_parallelism_unknown(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """None (no llama-server running yet, ps unavailable) means 'unknown',
    not '0': a first dispatch that loads the model must not false-warn.
    The warning is gated on a 2nd+ concurrent dispatch anyway, but the
    None guard is belt-and-suspenders so a degraded probe never reads as
    'parallelism is zero'."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"gpt-oss:20b"})
    monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: None)

    _write_manifest(plan_dir, "np_unknown", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1241))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("np_unknown", "S1")

    assert not any("ollama serving parallelism" in n for n in notes), \
        f"unknown parallelism must not warn, got: {notes}"


def test_dispatch_no_warn_when_serving_parallelism_meets_max_concurrent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When -np matches MAX_CONCURRENT_AGENTS, Ollama can actually serve
    that many concurrent decodes - no warning. This is the steady-state
    the operator wants: OLLAMA_NUM_PARALLEL kept in sync with
    MAX_CONCURRENT_AGENTS."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: {"gpt-oss:20b"})
    monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: 2)

    _write_manifest(plan_dir, "np_ok", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "model": "gpt-oss:20b"},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1242))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.dispatch_story("np_ok", "S1")

    assert not any("ollama serving parallelism" in n for n in notes), \
        f"adequate parallelism must not warn, got: {notes}"


# ---------- FM-B: reviewer rate-limit must defer, not consume rework budget ----------


def test_is_rate_limited_detects_session_limit():
    assert p._is_rate_limited(_RATE_LIMIT_MSG)


def test_is_rate_limited_detects_out_of_credits():
    assert p._is_rate_limited('{"overageDisabledReason":"out_of_credits"}')


def test_is_rate_limited_detects_usage_limit_reached():
    assert p._is_rate_limited("Usage limit reached. Your limit resets tomorrow.")


def test_is_rate_limited_false_for_normal_review():
    normal = (
        "I reviewed the diff. The implementation looks correct.\n"
        "VERDICT: APPROVE\n"
        "PR title: Fix retry logic\n"
    )
    assert not p._is_rate_limited(normal)


def test_is_rate_limited_false_for_review_mentioning_session_limit():
    # A reviewer discussing rate-limit code must NOT be treated as rate-limited.
    text = (
        "The session limit check on line 42 should raise ValueError, not return None.\n"
        "VERDICT: REQUEST_CHANGES"
    )
    assert not p._is_rate_limited(text)


def test_review_story_rate_limited_leaves_status_tests_passed(plan_dir, agents_dir, monkeypatch):
    # When the reviewer returns a rate-limit message, status must stay
    # tests_passed so the next advance_pipeline tick retries review.
    _write_manifest(plan_dir, "rl_defer", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)

    def _boom(*a, **k):
        raise AssertionError("PR must not be opened on rate-limit deferral")
    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("rl_defer", "S1")

    assert result["status"] == "tests_passed"
    assert result.get("deferred") == "rate_limited"
    story = _read_manifest(plan_dir, "rl_defer")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert "rework_attempts" not in story


def test_review_story_rate_limited_does_not_increment_rework_attempts(plan_dir, agents_dir, monkeypatch):
    # Even with prior rework cycles, a rate-limit hit must not count.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rl_noincr", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))

    p.review_story("rl_noincr", "S1")

    story = _read_manifest(plan_dir, "rl_noincr")["stories"]["S1"]
    assert story["rework_attempts"] == 2  # unchanged
    assert story["status"] == "tests_passed"


def test_review_story_rate_limited_notifies_user(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "rl_notify", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.review_story("rl_notify", "S1")

    assert any("rate" in n.lower() or "deferred" in n.lower() for n in notes)


def test_review_story_genuine_request_changes_still_increments_rework(plan_dir, agents_dir, monkeypatch):
    # Regression: a real REQUEST_CHANGES must still count against the budget.
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rl_regression", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "The error path is untested.\nVERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    p.review_story("rl_regression", "S1")

    story = _read_manifest(plan_dir, "rl_regression")["stories"]["S1"]
    assert story["rework_attempts"] == 1
    assert story["status"] == "changes_requested"


# ---------- T11: content-free REQUEST_CHANGES must not burn rework budget ----------

def test_review_story_bare_request_changes_is_treated_as_inconclusive(plan_dir, agents_dir, monkeypatch):
    # A REQUEST_CHANGES with no findings text gives the redispatched agent
    # nothing to act on - it must be treated like an inconclusive review
    # (retry, review_inconclusive_count), not a genuine rejection that burns
    # the rework budget.
    _write_manifest(plan_dir, "rc_empty", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on empty REQUEST_CHANGES")))
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )

    result = p.review_story("rc_empty", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "rc_empty")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert "rework_attempts" not in story
    assert "review_feedback" not in story
    assert story["review_inconclusive_count"] == 1
    assert any("no findings" in n.lower() or "empty" in n.lower() for n in notes)


def test_review_story_bare_request_changes_parks_after_max_inconclusive_attempts(plan_dir, agents_dir, monkeypatch):
    # Default max is 2: a second consecutive content-free REQUEST_CHANGES
    # must park for human review rather than retrying forever, and must
    # never open a PR or consume rework budget along the way.
    _write_manifest(plan_dir, "rc_empty_park", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    pr_calls = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: pr_calls.append(1))
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )

    result1 = p.review_story("rc_empty_park", "S1")
    assert result1["status"] == "tests_passed"

    result2 = p.review_story("rc_empty_park", "S1")

    assert result2["verdict"] == "REQUEST_CHANGES"
    assert result2["status"] == "parked"
    story = _read_manifest(plan_dir, "rc_empty_park")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["review_inconclusive_count"] == 2
    assert "inconclusive after 2 attempts" in story["parked_reason"]
    assert "rework_attempts" not in story
    assert pr_calls == [], "a content-free REQUEST_CHANGES must never open a PR"
    assert any("parked" in n.lower() for n in notes)


# ---------- Gap 5: Ollama 429 (RateLimitedError) on review path -> deferral ----------
def test_review_story_defers_on_ollama_rate_limited(plan_dir, agents_dir, monkeypatch):
    """When the reviewer raises backend.RateLimitedError (an Ollama 429),
    review_story must defer to the next tick (status stays tests_passed,
    review_deferred_count increments), NOT count as an inconclusive review
    and burn the rework budget. This mirrors the Claude rate-limit path
    but for the local-ollama cloud 429 case."""
    _write_manifest(plan_dir, "rl_ollama", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    def _raise_429(wt, br, **k):
        raise backend.RateLimitedError("simulated 429 from ollama-cloud")

    monkeypatch.setattr(p, "_run_reviewer", _raise_429)
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)
    # _open_pr must NOT be called on deferral.
    def _boom(*a, **k):
        raise AssertionError("PR must not be opened on rate-limit deferral")
    monkeypatch.setattr(p, "_open_pr", _boom)

    result = p.review_story("rl_ollama", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "rl_ollama")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["review_deferred_count"] == 1
    # Crucially: rework_attempts must NOT be touched, just like the Claude
    # rate-limit path. The whole point of routing 429 to deferral is that
    # a transient infra event doesn't penalize the implementation.
    assert "rework_attempts" not in story


def test_review_story_ollama_rate_limited_does_not_burn_inconclusive_budget(
    plan_dir, agents_dir, monkeypatch
):
    """A 429 must NOT increment review_inconclusive_count either. A misclassified
    429 would silently drain the inconclusive budget and eventually park a
    story that should just be retried next tick."""
    _write_manifest(plan_dir, "rl_ollama_noincr", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "review_inconclusive_count": 1},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: (_ for _ in ()).throw(
                            backend.RateLimitedError("simulated 429")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    p.review_story("rl_ollama_noincr", "S1")

    story = _read_manifest(plan_dir, "rl_ollama_noincr")["stories"]["S1"]
    # The pre-existing count is preserved; a 429 does not move it.
    assert story["review_inconclusive_count"] == 1
    assert "parked_reason" not in story
    assert story["status"] == "tests_passed"


def test_review_story_ollama_rate_limited_accumulates_deferred_count(
    plan_dir, agents_dir, monkeypatch
):
    """Repeated 429s (e.g. ollama-cloud weekly cap) must accumulate so the
    PIPELINE_REVIEW_FALLBACK_AFTER path can eventually fall over to a
    different review backend. The counter is the same one Claude's
    rate-limit path uses."""
    _write_manifest(plan_dir, "rl_ollama_acc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "review_deferred_count": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: (_ for _ in ()).throw(
                            backend.RateLimitedError("simulated 429")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    p.review_story("rl_ollama_acc", "S1")

    story = _read_manifest(plan_dir, "rl_ollama_acc")["stories"]["S1"]
    assert story["review_deferred_count"] == 3


def test_review_story_non_rate_limited_exception_still_falls_to_inconclusive(
    plan_dir, agents_dir, monkeypatch
):
    """Guard: a generic Exception (not RateLimitedError) on the review
    path must still take the existing inconclusive path. The new 429 branch
    must not swallow other failures."""
    _write_manifest(plan_dir, "rl_ollama_other", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    def _raise_other(wt, br, **k):
        raise ValueError("malformed tool call shape")

    monkeypatch.setattr(p, "_run_reviewer", _raise_other)
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    p.review_story("rl_ollama_other", "S1")

    story = _read_manifest(plan_dir, "rl_ollama_other")["stories"]["S1"]
    # A generic exception takes the inconclusive path: review_inconclusive_count
    # increments, status stays tests_passed for retry.
    assert story["review_inconclusive_count"] == 1
    assert story["status"] == "tests_passed"
    # And it must NOT be recorded as a rate-limit deferral.
    assert "review_deferred_count" not in story or story["review_deferred_count"] == 0


def test_review_story_high_risk_security_rate_limited_defers(plan_dir, agents_dir, monkeypatch):
    # A rate-limit hit on the security reviewer must also defer, not block.
    _write_manifest(plan_dir, "rl_sec", {
        "S1": {"summary": "Auth change", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_run_security_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on security defer")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.review_story("rl_sec", "S1")

    assert result["status"] == "tests_passed"
    assert result.get("deferred") == "rate_limited"
    story = _read_manifest(plan_dir, "rl_sec")["stories"]["S1"]
    assert "rework_attempts" not in story


def test_advance_pipeline_reports_review_deferred_on_rate_limit(plan_dir, agents_dir, monkeypatch):
    # FM-H: advance_pipeline must surface which stories had review deferred by
    # a reviewer rate-limit so the benchmark harness (or any caller ticking
    # advance_pipeline in a wall-clock loop) can extend its deadline instead
    # of burning budget while the reviewer is gated.
    _write_manifest(plan_dir, "defer_visible", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    # Pin the review resource gate open so the test exercises the rate-limit
    # deferral path, not the configured review backend's runtime availability
    # (which depends on the live model_registry.json review provider and free
    # memory).
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on defer")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.advance_pipeline("defer_visible")

    assert "S1" in result["review_deferred"]


def test_advance_pipeline_does_not_report_genuine_verdict_as_deferred(plan_dir, agents_dir, monkeypatch):
    # Negative case: a real REQUEST_CHANGES/APPROVE verdict is not a deferral
    # and must not appear in review_deferred.
    _write_manifest(plan_dir, "no_defer", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "The error path is untested.\nVERDICT: REQUEST_CHANGES")
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.advance_pipeline("no_defer")

    assert result["review_deferred"] == []
    assert "S1" not in result["review_deferred"]


# ---------- Local-backend reviewer fallback after repeated rate-limit ----------

def test_review_story_review_fallback_to_local_after_repeated_rate_limit(plan_dir, agents_dir, monkeypatch):
    # PIPELINE_REVIEW_FALLBACK=local + PIPELINE_REVIEW_FALLBACK_AFTER=2: the
    # 1st rate-limited call defers as usual; the 2nd rate-limited call crosses
    # the threshold and retries inline with the local backend in the same
    # review_story() invocation, resolving to a real verdict.
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK", "local")
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK_AFTER", "2")
    _write_manifest(plan_dir, "fb_local", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, backend_name=None, **k):
        calls.append(backend_name)
        if len(calls) <= 2:
            return _RATE_LIMIT_MSG
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://example.com/pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result1 = p.review_story("fb_local", "S1")
    assert result1.get("deferred") == "rate_limited"
    assert result1["status"] == "tests_passed"

    result2 = p.review_story("fb_local", "S1")
    assert result2.get("deferred") is None
    assert result2["status"] == "pr_open"
    assert result2["verdict"] == "APPROVE"

    assert calls == [None, None, "local"]
    story = _read_manifest(plan_dir, "fb_local")["stories"]["S1"]
    assert story["review_verdict"] == "APPROVE"
    assert story["review_deferred_count"] == 0


def test_review_story_review_fallback_to_explicit_provider_after_repeated_rate_limit(
    plan_dir, agents_dir, monkeypatch,
):
    # T16: PIPELINE_REVIEW_FALLBACK accepts an explicit provider name (not
    # just the "local" alias) and passes it straight through to
    # _run_reviewer's backend_name override.
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK", "lmstudio")
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK_AFTER", "2")
    _write_manifest(plan_dir, "fb_lmstudio", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, backend_name=None, **k):
        calls.append(backend_name)
        if len(calls) <= 2:
            return _RATE_LIMIT_MSG
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://example.com/pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    p.review_story("fb_lmstudio", "S1")
    result2 = p.review_story("fb_lmstudio", "S1")

    assert result2["status"] == "pr_open"
    assert calls == [None, None, "lmstudio"]


def test_review_story_fallback_disabled_by_default_keeps_deferring(plan_dir, agents_dir, monkeypatch):
    # Negative/boundary test: with PIPELINE_REVIEW_FALLBACK unset (default
    # "off"), FM-B's original behavior must be unchanged - every rate-limited
    # call defers, no matter how many times it happens, and the reviewer is
    # never invoked with the local backend override.
    monkeypatch.delenv("PIPELINE_REVIEW_FALLBACK", raising=False)
    _write_manifest(plan_dir, "fb_off", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, backend_name=None, **k):
        calls.append(backend_name)
        return _RATE_LIMIT_MSG

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    for _ in range(5):
        result = p.review_story("fb_off", "S1")
        assert result.get("deferred") == "rate_limited"
        assert result["status"] == "tests_passed"

    assert calls == [None, None, None, None, None]


def test_review_story_review_fallback_off_setting_keeps_deferring(plan_dir, agents_dir, monkeypatch):
    # Explicit PIPELINE_REVIEW_FALLBACK=off behaves identically to unset.
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK", "off")
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK_AFTER", "1")
    _write_manifest(plan_dir, "fb_explicit_off", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, backend_name=None, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.review_story("fb_explicit_off", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"


def test_review_story_deferred_count_resets_on_genuine_verdict(plan_dir, agents_dir, monkeypatch):
    # Fallback env NOT set: a genuine verdict following a rate-limit deferral
    # must reset the persisted counter back to 0, proving it doesn't
    # accumulate across unrelated recovery cycles.
    monkeypatch.delenv("PIPELINE_REVIEW_FALLBACK", raising=False)
    _write_manifest(plan_dir, "fb_reset", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, backend_name=None, **k):
        calls.append(backend_name)
        if len(calls) == 1:
            return _RATE_LIMIT_MSG
        return "The error path is untested.\nVERDICT: REQUEST_CHANGES"

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result1 = p.review_story("fb_reset", "S1")
    assert result1.get("deferred") == "rate_limited"
    story = _read_manifest(plan_dir, "fb_reset")["stories"]["S1"]
    assert story["review_deferred_count"] == 1

    result2 = p.review_story("fb_reset", "S1")
    assert result2["verdict"] == "REQUEST_CHANGES"
    story = _read_manifest(plan_dir, "fb_reset")["stories"]["S1"]
    assert story["review_deferred_count"] == 0


def test_review_story_review_fallback_after_one_triggers_on_first_deferral(plan_dir, agents_dir, monkeypatch):
    # Boundary: PIPELINE_REVIEW_FALLBACK_AFTER=1 crosses the threshold on the
    # very first rate-limited response (not the second), so a single
    # review_story() call both defers once and immediately retries locally.
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK", "local")
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK_AFTER", "1")
    _write_manifest(plan_dir, "fb_after_one", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, backend_name=None, **k):
        calls.append(backend_name)
        if backend_name == "local":
            return "VERDICT: APPROVE"
        return _RATE_LIMIT_MSG

    monkeypatch.setattr(p, "_run_reviewer", _stub)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://example.com/pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.review_story("fb_after_one", "S1")

    assert result.get("deferred") is None
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert calls == [None, "local"]


def test_review_story_high_risk_security_ignores_review_fallback(plan_dir, agents_dir, monkeypatch):
    # The security-engineer pass must keep deferring on rate-limit regardless
    # of PIPELINE_REVIEW_FALLBACK - security-engineer always runs on Claude
    # per backend.py's _LOCAL_SKIP_PERSONAS design. _run_security_reviewer
    # must never receive a backend_name override.
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK", "local")
    monkeypatch.setenv("PIPELINE_REVIEW_FALLBACK_AFTER", "1")
    _write_manifest(plan_dir, "fb_sec", {
        "S1": {"summary": "Auth change", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "high"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, backend_name=None, **k: "VERDICT: APPROVE")
    sec_calls = []

    def _sec_stub(wt, br, **k):
        sec_calls.append((wt, br))
        return _RATE_LIMIT_MSG

    monkeypatch.setattr(p, "_run_security_reviewer", _sec_stub)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on security defer")))
    monkeypatch.setattr(p, "_notify_user", lambda *a: None)

    result = p.review_story("fb_sec", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"
    assert len(sec_calls) == 1
    story = _read_manifest(plan_dir, "fb_sec")["stories"]["S1"]
    assert "rework_attempts" not in story


