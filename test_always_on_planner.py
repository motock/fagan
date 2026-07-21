"""Tests for the always-on guided-decomposition planner refactor.

These tests verify the changes described in the always-on checklist plan:
  - `_resolve_planner_backend` no longer takes a `mode` parameter; the
    `cloud` branch and the mirror-dispatch short-circuit are gone. It now
    delegates to `role_registry.resolve_role('planner', ...)` with
    `default_provider='ollama'` and a concrete-tag `model_fallback`.
  - A `PIPELINE_LOCAL_PLANNER_MODEL` env override mirrors
    `PIPELINE_LOCAL_REVIEW_MODEL`, gated on the resolved provider being a
    local-family backend.
  - `_run_planner` / `_run_rework_planner` no longer take `mode`.
  - `dispatch_story` always runs the planner for a local-family dispatch
    when `PIPELINE_DECOMPOSE` is unset (always-on), with no `cloud`/`local`
    mode conjunct.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q test_always_on_planner.py
"""

import json

import pytest

import backend
import role_registry
from pipeline import server as p
from pipeline import ticketing as pt
import pipeline_mcp_server  # noqa: F401  backward compat


# ---------- helpers shared with the main test suite ----------

class _FakePlannerBackend:
    """Stand-in for whatever backend.get_backend(...) returns, capturing the
    exact kwargs _run_planner passed to complete()."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


# ---------- fixtures (mirror the main suite's contracts) ----------

@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    from pipeline import persona as pper
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


def _stub_dispatch_externals(monkeypatch):
    """Stub the external boundaries dispatch_story touches so it can run to
    completion without git/gh/Plane/subprocess."""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env=None, **kw: _FakeProc(9500))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


# ---------- (a) registry default: ollama/glm-5.2:cloud, no env, no role_config ----------

def test_resolve_planner_backend_defaults_to_ollama_glm_no_env_no_role_config(monkeypatch):
    """With NO env vars and NO plan_role_config, the planner must resolve to
    ollama/glm-5.2:cloud via the registry's roles.planner entry (the stock
    production registry pins planner=ollama/glm). This is the always-on
    default — no PIPELINE_DECOMPOSE mode flag involved."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    # Use the REAL registry (not an empty stub) so the stock roles.planner
    # entry is exercised.
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "ollama"
    assert model == "glm-5.2:cloud"


# ---------- (b) no roles.planner registry entry → ollama/glm via fallback ----------

def test_resolve_planner_backend_no_registry_entry_falls_back_to_glm_tag(monkeypatch):
    """When the registry has NO roles.planner entry at all, the planner must
    still resolve to ollama/glm (the concrete tag glm-5.2:cloud), NOT mirror
    dispatch_backend/local_model. The model_fallback must be the CONCRETE tag
    (glm-5.2:cloud), not the friendly name 'glm', because resolve_role's
    model_fallback path returns it verbatim without resolving against
    providers.<p>.models."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    # Empty registry: no roles.planner entry, but providers.ollama.models.glm
    # still declared so the fallback tag is verifiable.
    empty_registry = {
        "providers": {
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "gpt-oss": {"tag": "gpt-oss:20b"},
                },
            },
        },
        "roles": {},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: empty_registry)
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "ollama"
    # MUST be the concrete tag, not the friendly name "glm".
    assert model == "glm-5.2:cloud"
    assert model != "glm"


# ---------- (c) PIPELINE_BACKEND_PLANNER pins provider, PIPELINE_LOCAL_PLANNER_MODEL pins model ----------

def test_resolve_planner_backend_env_provider_and_local_model_override_together(monkeypatch):
    """PIPELINE_BACKEND_PLANNER=mlx pins the provider, and
    PIPELINE_LOCAL_PLANNER_MODEL pins the model — together overriding the
    registry's default ollama/glm. Mirrors PIPELINE_LOCAL_REVIEW_MODEL."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "mlx")
    monkeypatch.setenv("PIPELINE_LOCAL_PLANNER_MODEL", "custom-mlx-model")
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "mlx"
    assert model == "custom-mlx-model"


# ---------- (d) PIPELINE_LOCAL_PLANNER_MODEL ignored when provider is claude ----------

def test_resolve_planner_backend_local_model_ignored_for_claude_provider(monkeypatch):
    """PIPELINE_LOCAL_PLANNER_MODEL must be IGNORED when the resolved
    provider is claude (not a local-family backend) — a bare Ollama tag must
    never leak into a Claude planner as a bogus --model value."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    # A distinct tag (not the glm fallback) so the "ignored" assertion is
    # meaningful: if the override leaked, model would equal this exact value.
    monkeypatch.setenv("PIPELINE_LOCAL_PLANNER_MODEL", "qwen3-coder:30b")
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "claude"
    # The local-model override must NOT have been applied to a claude planner.
    assert model != "qwen3-coder:30b"


# ---------- (e) plan role_config.planner wins over env and registry ----------

def test_resolve_planner_backend_plan_role_config_wins_over_env_provider_and_registry(monkeypatch):
    """plan_role_config['planner'].provider wins over PIPELINE_BACKEND_PLANNER
    and the registry; plan_role_config['planner'].model wins over the registry
    default. PIPELINE_LOCAL_PLANNER_MODEL is the separate top-priority *model*
    override (mirrored from review.py: it wins over role_config and registry,
    exercised in test_resolve_planner_backend_local_planner_model_env_wins)."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "ollama")
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"planner": {"provider": "mlx", "model": "qwen"}},
    )
    assert backend_name == "mlx"
    # qwen is the repo-root registry's declared mlx model → resolved to its tag.
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


# ---------- (f) planner always runs for local-family dispatch when PIPELINE_DECOMPOSE unset ----------

def test_dispatch_story_planner_always_runs_for_local_when_decompose_unset(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With PIPELINE_DECOMPOSE unset (always-on), a local-family dispatch
    must still run the planner — no mode flag required. The planner must be
    called (not skipped), and the plan written to disk."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_DECOMPOSE", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    # Do NOT pre-create the worktree: dispatch creates it itself (server.py
    # worktree_path.mkdir), and pre-creation would make worktree_path.exists()
    # True at the resuming check -> resuming=True -> initial planner skipped.
    worktree_path = worktree_root / "S1"
    _write_manifest(plan_dir, "aoplanner", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append({"agent_instructions": agent_instructions, **kwargs})
        return "1. Write a failing test.\n2. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("aoplanner", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    assert planner_calls[0]["agent_instructions"] == "Build it."
    # The plan must be written to disk.
    assert (worktree_path / ".agent_plan.md").exists()


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

    # Without mode — must succeed.
    backend_name, model = p._resolve_planner_backend("ollama", "gpt-oss:20b")
    assert backend_name == "ollama"

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


# ---------- get_role_config shows planner resolving to ollama/glm-5.2:cloud ----------

def test_get_role_config_planner_resolves_to_ollama_glm(monkeypatch):
    """get_role_config(plan_name=None) must show the planner role resolving
    to ollama/glm-5.2:cloud (the concrete tag), not a friendly name and not
    a mirror-dispatch placeholder."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    result = p.get_role_config(plan_name=None)
    assert result["ok"] is True
    planner = result["roles"]["planner"]
    assert planner["provider"] == "ollama"
    assert planner["model"] == "glm-5.2:cloud"


# ---------- no PIPELINE_DECOMPOSE / PIPELINE_DECOMPOSE_CLOUD_MODEL references remain ----------

def test_no_pipeline_decompose_mode_references_in_planner_module():
    """The planner module must not reference PIPELINE_DECOMPOSE (as its own
    var, not the scratchpad) or PIPELINE_DECOMPOSE_CLOUD_MODEL or
    mode == 'cloud' or decompose_mode."""
    import pipeline.planner as planner_mod
    import inspect
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