"""Tests for the always-on guided-decomposition planner refactor (fail-open
provider handling, no-`mode`-parameter regression checks, negative dispatch
paths, and role-config surfacing).

Split out of test_always_on_planner.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._always_on_planner_helpers.
"""
import json

import pytest

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
    role_registry,
)
from pipeline import server as p
from tests.unit._always_on_planner_helpers import (  # noqa: F401
    _clear_caches,
    _FakePlannerBackend,
    _FakeProc,
    _plane_configured,
    _stub_dispatch_externals,
    _write_manifest,
    agents_dir,
    plan_dir,
    worktree_root,
)

# ---------- (g) garbage/unknown PIPELINE_BACKEND_PLANNER fails open, never crashes ----------

def test_resolve_planner_backend_garbage_provider_fails_open_no_crash(monkeypatch):
    """A garbage/unknown PIPELINE_BACKEND_PLANNER value must fail open —
    _run_planner's except-Exception fail-open contract means dispatch never
    crashes. _resolve_planner_backend itself may raise (resolve_role
    validates the model against the provider's declared models), but
    _run_planner must catch it and return None."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "nonexistent-provider")
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    fake = _FakePlannerBackend(response="should never get here")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    # _run_planner must NOT raise — it must return None (fail-open).
    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="ollama", local_model="gpt-oss:20b",
    )
    assert result is None
    # The backend's complete() must never have been called.
    assert fake.calls == []


def test_run_rework_planner_garbage_provider_fails_open_no_crash(monkeypatch):
    """A garbage/unknown PIPELINE_BACKEND_PLANNER value must fail open on the
    REWORK path too — _run_rework_planner must catch the RoleRegistryError
    raised by _resolve_planner_backend (resolve is inside the try, mirroring
    _run_planner) and return None, never crashing the rework redispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "nonexistent-provider")
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    fake = _FakePlannerBackend(response="should never get here")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    # _run_rework_planner must NOT raise — it must return None (fail-open).
    result = p._run_rework_planner(
        "allow() double-counts refill.", dispatch_backend="ollama",
        local_model="gpt-oss:20b",
    )
    assert result is None
    # The backend's complete() must never have been called.
    assert fake.calls == []


# ---------- _run_planner / _run_rework_planner no longer take `mode` ----------

def test_run_planner_no_mode_parameter(agents_dir, monkeypatch):
    """_run_planner must NOT accept a `mode` keyword argument — the mode
    parameter has been removed. Calling without mode must work; calling
    WITH mode must raise TypeError."""
    fake = _FakePlannerBackend(response="1. Step one")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)

    # Without mode — must succeed.
    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="ollama", local_model="gpt-oss:20b",
    )
    assert result == "1. Step one"

    # With mode — must raise TypeError (parameter removed).
    with pytest.raises(TypeError):
        p._run_planner(
            "Add a rate limiter.", mode="cloud",
            dispatch_backend="ollama", local_model="gpt-oss:20b",
        )


def test_run_rework_planner_no_mode_parameter(agents_dir, monkeypatch):
    """_run_rework_planner must NOT accept a `mode` keyword argument."""
    fake = _FakePlannerBackend(response="1. Fix step")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)

    result = p._run_rework_planner(
        "allow() double-counts refill.", dispatch_backend="ollama",
        local_model="gpt-oss:20b",
    )
    assert result == "1. Fix step"

    with pytest.raises(TypeError):
        p._run_rework_planner(
            "allow() double-counts refill.", mode="cloud",
            dispatch_backend="ollama", local_model="gpt-oss:20b",
        )


# ---------- _resolve_planner_backend no longer takes `mode` ----------

def test_resolve_planner_backend_no_mode_parameter(monkeypatch):
    """_resolve_planner_backend must NOT accept a `mode` keyword argument."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)

    # Without mode — must succeed. The point of this assertion is just "the
    # call succeeded", not any specific provider/model, so stub the registry
    # rather than depend on whatever model_registry.json says today.
    fake_registry = {
        "providers": {"acme": {"models": {"widget": {"tag": "widget-v1"}}}},
        "roles": {"planner": {"provider": "acme", "model": "widget"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    backend_name, _model = p._resolve_planner_backend("ollama", "gpt-oss:20b")
    assert backend_name == "acme"

    # With mode keyword — must raise TypeError (parameter removed).
    with pytest.raises(TypeError):
        p._resolve_planner_backend("ollama", "gpt-oss:20b", mode="cloud")


# ---------- negative: resuming dispatch still skips the planner ----------

def test_dispatch_story_resuming_skips_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A resuming dispatch (status=changes_requested with a transcript) must
    NOT run the initial _run_planner — the planner runs once on the story's
    first dispatch, never on a rework redispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "resume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "Fix the bug."},
    })

    def _boom_planner(*a, **k):
        raise AssertionError("_run_planner must not run on a resuming dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom_planner)
    # The rework planner CAN run on resume — stub it to avoid the real call.
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("resume", "S1")
    assert result["ok"] is True


# ---------- negative: plan_path.exists() still skips re-planning ----------

def test_dispatch_story_existing_plan_skips_replanning(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """If .agent_plan.md already exists in the worktree, the planner must
    NOT be called again (belt-and-suspenders with the `resuming` guard)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_plan.md").write_text("1. Pre-existing plan.\n")
    _write_manifest(plan_dir, "existingplan", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom_planner(*a, **k):
        raise AssertionError("_run_planner must not run when a plan already exists")

    monkeypatch.setattr(p, "_run_planner", _boom_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("existingplan", "S1")
    assert result["ok"] is True


# ---------- negative: Claude dispatch never triggers the planner ----------

def test_dispatch_story_claude_backend_skips_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The planner crutch exists for the weak local executor only — a Claude
    dispatch must never trigger planning even though the planner is now
    always-on for local-family backends."""
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    # Default dispatch backend is claude (no env override).
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _write_manifest(plan_dir, "claudeonly", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("claudeonly", "S1")
    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".agent_plan.md").exists()


# ---------- PIPELINE_DECOMPOSE_SCRATCHPAD still respected ----------

def test_dispatch_story_scratchpad_env_still_respected(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD is NOT being removed — it must still
    control whether the scratchpad instruction is included, even though
    PIPELINE_DECOMPOSE mode is gone."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", "off")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    _write_manifest(plan_dir, "scratchoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    captured = {}

    def _fake_planner(agent_instructions, **kwargs):
        captured.update(kwargs)
        return "1. Write a failing test.\n2. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("scratchoff", "S1")
    assert result["ok"] is True
    # include_scratchpad must be False when PIPELINE_DECOMPOSE_SCRATCHPAD=off.
    assert captured.get("include_scratchpad") is False


# ---------- get_role_config shows planner resolving to the registry entry ----------

def test_get_role_config_planner_resolves_to_registry_planner_entry(monkeypatch):
    """get_role_config(plan_name=None) must show the planner role resolving
    to whatever roles.planner says in the registry - stubbed here so the
    test doesn't depend on which provider/model model_registry.json
    currently configures (see
    test_resolve_planner_backend_defaults_to_registry_planner_entry_no_env_no_role_config)."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    fake_registry = {
        "providers": {"acme": {"models": {"widget": {"tag": "widget-v1"}}}},
        "roles": {"planner": {"provider": "acme", "model": "widget"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    result = p.get_role_config(plan_name=None)
    assert result["ok"] is True
    planner = result["roles"]["planner"]
    assert planner["provider"] == "acme"
    assert planner["model"] == "widget-v1"


# ---------- no PIPELINE_DECOMPOSE / PIPELINE_DECOMPOSE_CLOUD_MODEL references remain ----------

def test_no_pipeline_decompose_mode_references_in_planner_module():
    """The planner module must not reference PIPELINE_DECOMPOSE (as its own
    var, not the scratchpad) or PIPELINE_DECOMPOSE_CLOUD_MODEL or
    mode == 'cloud' or decompose_mode."""
    import inspect

    import pipeline.planner as planner_mod
    source = inspect.getsource(planner_mod)
    assert "PIPELINE_DECOMPOSE_CLOUD_MODEL" not in source
    assert "mode == \"cloud\"" not in source
    assert "mode == 'cloud'" not in source
    # PIPELINE_DECOMPOSE_SCRATCHPAD is fine; bare PIPELINE_DECOMPOSE is not.
    for line in source.splitlines():
        stripped = line.strip()
        # Allow PIPELINE_DECOMPOSE_SCRATCHPAD but not bare PIPELINE_DECOMPOSE.
        if "PIPELINE_DECOMPOSE" in stripped and "SCRATCHPAD" not in stripped:
            pytest.fail(f"Unexpected PIPELINE_DECOMPOSE reference in planner.py: {stripped}")


def test_no_decompose_mode_in_server_module():
    """The server module must not reference decompose_mode or
    PIPELINE_DECOMPOSE (as its own var, not the scratchpad) or
    mode=decompose_mode."""
    import inspect
    source = inspect.getsource(p)
    assert "decompose_mode" not in source
    for line in source.splitlines():
        stripped = line.strip()
        if "PIPELINE_DECOMPOSE" in stripped and "SCRATCHPAD" not in stripped:
            pytest.fail(f"Unexpected PIPELINE_DECOMPOSE reference in server.py: {stripped}")


# ===========================================================================
# Checklist reuse gating: backend + hash sidecar (stale-checklist fix)
# ===========================================================================
#
# The REUSE block (the `if plan_path.exists():` branch that injects an existing
# .agent_plan.md into spec["prompt"]) was previously guarded ONLY by
# plan_path.exists() -- no backend check, no staleness check. Two bugs flowed
# from that:
#   (a) a story escalated to `backend: claude` still got a local-only checklist
#       injected if a leftover .agent_plan.md sat in the worktree from an
#       earlier local attempt (contradicts the generation guard's "Claude
#       doesn't need the crutch" comment).
#   (b) patch_story can rewrite agent_instructions, but the cached checklist on
#       disk was never invalidated -- the executor got NEW instructions in the
#       prompt body AND the OLD, contradictory checklist appended after it.
#
# The fix:
#   - adds `import hashlib` near the top of pipeline/server.py,
#   - tracks a `plan_hash_path = worktree_path / ".agent_plan_src_hash"` sidecar
#     alongside plan_path,
#   - writes a sha256 of the story's agent_instructions to that sidecar whenever
#     a fresh checklist is generated,
#   - gates the REUSE block on BOTH a local-family backend AND a hash of the
#     CURRENT agent_instructions matching the sidecar.
#
# These tests capture the actual composed prompt text (not just dispatch ok) by
# stubbing backend.subprocess.Popen with a capturing stub, then asserting on the
# prompt embedded in the cmd (claude: cmd[2], the `-p` argument) or the env
# (local: env["LOCAL_AGENT_TASK"]).


def _captured_prompt(cmd, env):
    """Extract the composed prompt text from a captured Popen invocation.

    Claude CLI: cmd == ["claude", "-p", prompt, "--model", model, ...] so the
    prompt is the element immediately after "-p". Local (ollama) driver: the
    prompt is carried in the LOCAL_AGENT_TASK env var, not in argv.
    """
    if "-p" in cmd:
        idx = cmd.index("-p")
        return cmd[idx + 1]
    if env and "LOCAL_AGENT_TASK" in env:
        return env["LOCAL_AGENT_TASK"]
    raise AssertionError(
        f"could not locate prompt in captured cmd={cmd!r} env_keys="
        f"{list(env.keys()) if env else None}"
    )


def _capture_popen_factory(captured):
    """Return a Popen stub that records (cmd, env) into `captured` and returns
    a _FakeProc so dispatch_story completes normally."""

    def _capture_popen(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeProc(9500)

    return _capture_popen


def test_server_module_imports_hashlib():
    """The fix adds `import hashlib` to pipeline/server.py (alphabetically
    between fcntl and json). Assert the module imports it so the generation
    and reuse guards can compute the agent_instructions sha256."""
    import inspect

    source = inspect.getsource(p)
    # The exact three-line block the task specifies.
    assert "import ast\nimport fcntl\nimport hashlib\nimport json\n" in source


def test_server_module_defines_plan_hash_sidecar_path():
    """The fix tracks a `.agent_plan_src_hash` sidecar path alongside
    plan_path, and the REUSE guard must reference it. Assert both the sidecar
    path literal and the new guard variable appear in the server source."""
    import inspect

    from pipeline import dispatch

    source = inspect.getsource(dispatch)
    assert 'plan_hash_path = worktree_path / ".agent_plan_src_hash"' in source
    # The new guard variable name from the fix.
    assert "checklist_is_fresh" in source
    assert "current_instructions_hash" in source
    # The REUSE comment must now mention the backend+hash guard.
    assert "local-family backend" in source
    assert "matching" in source


def test_dispatch_story_claude_backend_skips_stale_local_checklist(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Bug (a): a story escalated to `backend: claude` must NOT get a leftover
    local-only checklist injected, even when .agent_plan.md physically exists
    in the worktree from an earlier local attempt. The REUSE block must be
    gated on a local-family backend."""
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    # Default dispatch backend is claude (no env override), matching
    # test_dispatch_story_claude_backend_skips_planner's pattern.
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    # Leftover local-only checklist from an earlier local attempt -- and
    # deliberately NO .agent_plan_src_hash sidecar (pre-fix worktree).
    (worktree_path / ".agent_plan.md").write_text(
        "1. Some stale local-only checklist step.\n"
    )
    _write_manifest(plan_dir, "clstale", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    _stub_dispatch_externals(monkeypatch)
    # Override the bare Popen stub with a capturing one so we can inspect the
    # composed prompt (the bare _stub_dispatch_externals Popen stub discards it).
    captured = {}
    monkeypatch.setattr(backend.subprocess, "Popen", _capture_popen_factory(captured))

    result = p.dispatch_story("clstale", "S1")
    assert result["ok"] is True

    prompt = _captured_prompt(captured["cmd"], captured.get("env"))
    assert "Implementation checklist from your tech lead" not in prompt
    assert "Some stale local-only checklist step" not in prompt


def test_dispatch_story_stale_checklist_hash_mismatch_skips_injection(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Bug (b): when patch_story rewrites agent_instructions, the cached
    checklist on disk must be silently dropped (not injected) because its hash
    no longer matches the CURRENT agent_instructions. Set backend to local so
    the ONLY failing condition is the hash mismatch."""
    import hashlib

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_plan.md").write_text("1. Old wrong step.\n")
    # A hash that does NOT match the manifest's current agent_instructions.
    (worktree_path / ".agent_plan_src_hash").write_text(
        hashlib.sha256(b"some old instructions").hexdigest()
    )
    _write_manifest(plan_dir, "hashmismatch", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "Build it, corrected.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError(
            "planner must not run when a plan already exists"
        )

    monkeypatch.setattr(p, "_run_planner", _boom)
    _stub_dispatch_externals(monkeypatch)
    captured = {}
    monkeypatch.setattr(backend.subprocess, "Popen", _capture_popen_factory(captured))

    result = p.dispatch_story("hashmismatch", "S1")
    assert result["ok"] is True

    prompt = _captured_prompt(captured["cmd"], captured.get("env"))
    assert "Implementation checklist from your tech lead" not in prompt
    assert "Old wrong step" not in prompt


def test_dispatch_story_fresh_checklist_hash_match_still_injects(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression protection for the still-working case: when the sidecar hash
    DOES match the story's current agent_instructions AND the backend is
    local-family, the existing checklist must STILL be injected (the fix must
    not over-suppress). Reuses the canonical 'Build it.' agent_instructions
    string other tests in this file use for this scenario."""
    import hashlib

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_plan.md").write_text("1. Current correct step.\n")
    # A hash that DOES match the manifest's agent_instructions value of "Build it.".
    (worktree_path / ".agent_plan_src_hash").write_text(
        hashlib.sha256(b"Build it.").hexdigest()
    )
    _write_manifest(plan_dir, "hashmatch", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError(
            "planner must not run when a plan already exists"
        )

    monkeypatch.setattr(p, "_run_planner", _boom)
    _stub_dispatch_externals(monkeypatch)
    captured = {}
    monkeypatch.setattr(backend.subprocess, "Popen", _capture_popen_factory(captured))

    result = p.dispatch_story("hashmatch", "S1")
    assert result["ok"] is True

    prompt = _captured_prompt(captured["cmd"], captured.get("env"))
    assert "Implementation checklist from your tech lead" in prompt
    assert "Current correct step" in prompt