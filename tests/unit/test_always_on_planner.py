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

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
    role_registry,
)
from pipeline import server as p
from pipeline import ticketing as pt

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


# ---------- (a) registry default: ollama/glm, no env, no role_config ----------

def test_resolve_planner_backend_defaults_to_registry_planner_entry_no_env_no_role_config(monkeypatch):
    """With NO env vars and NO plan_role_config, the planner must resolve to
    the registry's roles.planner entry. While Claude usage is capped the
    stock roles.planner entry is ollama/glm, which resolve_role resolves to
    its concrete driver tag glm-5.2:cloud. This is the always-on default,
    exercised against the REAL registry (not an empty stub) so the stock
    roles.planner entry is covered. Reverting the registry to claude/sonnet
    requires reverting these assertions too."""
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
    # The fix writes a hash sidecar alongside the plan so a later dispatch can
    # detect a stale checklist after a patch_story rewrite of agent_instructions.
    assert (worktree_path / ".agent_plan_src_hash").exists()


# ---------- tests_already_authored: planner must not tell the executor to
# write tests when a test-author phase already committed them ----------
#
# Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2: _PLANNER_SYSTEM's
# base "preserve TDD ordering" instruction is unconditional, so the glm-driven
# checklist told the gpt-oss:20b executor to "Create the NEW file test_ci_
# rework_feedback.py" - a file the test-author phase had already committed -
# a directly contradictory brief the executor had no reliable way to resolve.

def test_planner_system_default_unchanged_when_tests_not_authored():
    """tests_already_authored defaults to False and must not alter
    _planner_system's output at all (preserves the H3 ablation guarantee and
    every existing by-reference test of _PLANNER_SYSTEM)."""
    assert p._planner_system() == p._PLANNER_SYSTEM
    assert p._planner_system(tests_already_authored=False) == p._PLANNER_SYSTEM


def test_planner_system_tests_already_authored_adds_override_clause():
    system = p._planner_system(tests_already_authored=True)
    assert p._TEST_AUTHOR_ALREADY_RAN_CLAUSE in system
    # Base steering (don't touch test files) must still be present - the
    # override clause supplements it, it doesn't replace the whole prompt.
    assert "implementation file" in system
    # The clause must explicitly tell the planner not to emit a
    # write-the-test-file step.
    assert "already" in p._TEST_AUTHOR_ALREADY_RAN_CLAUSE.lower()
    assert "do not include" in p._TEST_AUTHOR_ALREADY_RAN_CLAUSE.lower()


def test_planner_system_tests_already_authored_composes_with_scratchpad():
    """Both clauses can be on at once (a split story with the scratchpad
    ablation also on) - order doesn't matter for correctness, just presence."""
    system = p._planner_system(tests_already_authored=True, include_scratchpad=True)
    assert p._TEST_AUTHOR_ALREADY_RAN_CLAUSE in system
    assert p._PLANNER_SCRATCHPAD_CLAUSE in system


def test_run_planner_passes_tests_already_authored_through_to_system(monkeypatch):
    fake = _FakePlannerBackend(response="1. Implement it.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)

    p._run_planner(
        "Fix the thing.", dispatch_backend="ollama", local_model="gpt-oss:20b",
        tests_already_authored=True,
    )

    assert p._TEST_AUTHOR_ALREADY_RAN_CLAUSE in fake.calls[0]["system"]


def test_dispatch_story_planner_told_tests_already_authored_when_phase_succeeded(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When the test-author phase runs and succeeds THIS dispatch (marker
    written before the planner call), _run_planner must be called with
    tests_already_authored=True - not left at the False default."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "taplanner", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("taplanner", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    assert planner_calls[0]["tests_already_authored"] is True


def test_dispatch_story_planner_told_tests_not_authored_when_phase_skipped(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When no test-author phase ran (not opted in), _run_planner must be
    called with tests_already_authored=False - the ordinary unsplit case
    must be unaffected by this change."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "notaplanner", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("notaplanner", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    assert planner_calls[0]["tests_already_authored"] is False


# ---------- authored_test_files: ground the planner in the test-author's
# ACTUAL committed files (strengthening _TEST_AUTHOR_ALREADY_RAN_CLAUSE) ----------
#
# Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset:
# the prohibition-only _TEST_AUTHOR_ALREADY_RAN_CLAUSE told the planner not
# to emit a write-the-test-file step, but gave it no concrete grounding in
# which file/tests actually exist on the branch. Even sonnet, planning from
# the story's own agent_instructions (which still describe the pre-split TDD
# flow verbatim), re-derived a "Write test_ci_rework_feedback.py" step with
# INVENTED test-case names that did not match the ones the test-author had
# committed - handing the executor a contradictory brief. The fix passes
# the test-author's actual committed file names + test-case names into the
# planner's system prompt as concrete grounding, so the planner names the
# real file/tests and points the executor at READING them instead of
# re-deriving them. authored_test_files is a list of (file_path, [test_names]).

def test_planner_system_default_unchanged_when_no_authored_files_even_if_flag():
    """authored_test_files defaults to None and must not alter
    _planner_system's base output when tests_already_authored is False
    (preserve every existing by-reference test of _PLANNER_SYSTEM)."""
    assert p._planner_system(
        tests_already_authored=False, authored_test_files=None,
    ) == p._PLANNER_SYSTEM


def test_planner_system_tests_already_authored_without_files_keeps_prohibition_only():
    """tests_already_authored=True with no authored_test_files (git could not
    detect them, or the test-author committed no test_*.py) falls back to
    the prohibition-only clause - behavior is unchanged from the prior fix,
    so a git-detection failure degrades gracefully rather than producing a
    malformed prompt."""
    system = p._planner_system(tests_already_authored=True, authored_test_files=None)
    assert p._TEST_AUTHOR_ALREADY_RAN_CLAUSE in system
    # No grounding section because no files were passed.
    assert "CONCRETE GROUNDING" not in system
    # And an empty list is treated the same as None.
    system_empty = p._planner_system(
        tests_already_authored=True, authored_test_files=[],
    )
    assert "CONCRETE GROUNDING" not in system_empty


def test_planner_system_authored_files_requires_tests_already_authored_flag():
    """authored_test_files passed WITHOUT tests_already_authored=True must NOT
    add the grounding clause - the two go together (grounding only applies to
    a split story whose test-author phase ran). Prevents a caller from
    accidentally grounding an unsplit dispatch."""
    system = p._planner_system(
        tests_already_authored=False,
        authored_test_files=[("test_foo.py", ["test_a"])],
    )
    assert system == p._PLANNER_SYSTEM


def test_planner_system_authored_files_names_file_and_tests_verbatim():
    """The grounding clause must name BOTH the file AND every test-case name
    verbatim, so the planner can reference them instead of inventing names
    from the task description - the exact live failure mode."""
    system = p._planner_system(
        tests_already_authored=True,
        authored_test_files=[("test_ci_rework_feedback.py", [
            "test_lint_gate_error_contains_gate_error_verbatim",
            "test_lint_gate_error_gives_lint_instruction_not_test_instruction",
        ])],
    )
    assert "test_ci_rework_feedback.py" in system
    assert "test_lint_gate_error_contains_gate_error_verbatim" in system
    assert "test_lint_gate_error_gives_lint_instruction_not_test_instruction" in system


def test_planner_system_authored_files_forbids_inventing_names():
    """The clause must explicitly forbid the planner from inventing or
    re-deriving test file/case names - the live failure mode was sonnet
    re-deriving wrong test-case names from agent_instructions."""
    system = p._planner_system(
        tests_already_authored=True,
        authored_test_files=[("test_foo.py", ["test_a"])],
    )
    lowered = system.lower()
    assert "invent" in lowered or "re-derive" in lowered or "rederive" in lowered


def test_planner_system_authored_files_directs_executor_to_read_first():
    """The grounding clause must make the checklist's first implementation
    step be to READ the committed test file(s) to learn the spec, not
    write them."""
    system = p._planner_system(
        tests_already_authored=True,
        authored_test_files=[("test_foo.py", ["test_a"])],
    )
    lowered = system.lower()
    assert "read" in lowered
    assert "test_foo.py" in system


def test_planner_system_authored_files_composes_with_scratchpad():
    """All three augmentations (prohibition, grounding, scratchpad) can be on
    at once for a split scratchpad story - just presence matters."""
    system = p._planner_system(
        tests_already_authored=True,
        authored_test_files=[("test_foo.py", ["test_a"])],
        include_scratchpad=True,
    )
    assert p._TEST_AUTHOR_ALREADY_RAN_CLAUSE in system
    assert "test_foo.py" in system
    assert p._PLANNER_SCRATCHPAD_CLAUSE in system


def test_run_planner_passes_authored_test_files_through_to_system(monkeypatch):
    fake = _FakePlannerBackend(response="1. Implement it.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)

    p._run_planner(
        "Fix the thing.", dispatch_backend="ollama", local_model="gpt-oss:20b",
        tests_already_authored=True,
        authored_test_files=[("test_foo.py", ["test_a", "test_b"])],
    )

    system = fake.calls[0]["system"]
    assert "test_foo.py" in system
    assert "test_a" in system
    assert "test_b" in system


def test_dispatch_story_passes_authored_test_files_to_planner_when_phase_succeeded(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When the test-author phase succeeds and commits test files on the
    branch, dispatch_story must pass the detected authored test files
    (file + test names) into _run_planner, so the planner is grounded in
    the real committed work - not just told to suppress a write-tests step."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "grounding", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)
    monkeypatch.setattr(p, "_test_files_added_on_branch",
                        lambda wt, base: ["test_foo.py"])
    monkeypatch.setattr(p, "_test_names_in_file",
                        lambda wt, rel: ["test_a", "test_b"])

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("grounding", "S1")

    assert result["ok"] is True
    assert planner_calls[0]["tests_already_authored"] is True
    assert planner_calls[0]["authored_test_files"] == [
        ("test_foo.py", ["test_a", "test_b"]),
    ]


def test_dispatch_story_passes_empty_authored_files_when_git_detects_none(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When the test-author phase ran but git detects no added test_*.py
    (e.g. the test-author committed a non-matching file name, or git
    failed), the planner must still be called with tests_already_authored
    True and an empty authored list - degrading to the prohibition-only
    clause, never crashing dispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "groundingempty", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)
    monkeypatch.setattr(p, "_test_files_added_on_branch", lambda wt, base: [])

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("groundingempty", "S1")

    assert result["ok"] is True
    assert planner_calls[0]["tests_already_authored"] is True
    # Empty list (not None) - git ran and found nothing.
    assert planner_calls[0]["authored_test_files"] == []


def test_dispatch_story_no_authored_files_when_phase_skipped(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When the test-author phase did NOT run (not opted in), the planner
    must be called with tests_already_authored=False and authored_test_files
    falsy - the ordinary unsplit case must be unaffected."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "groundingskip", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    planner_calls = []

    def _fake_planner(agent_instructions, **kwargs):
        planner_calls.append(kwargs)
        return "1. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("groundingskip", "S1")

    assert result["ok"] is True
    assert planner_calls[0]["tests_already_authored"] is False
    assert not planner_calls[0]["authored_test_files"]


def test_dispatch_story_prompt_disregards_stale_write_tests_instruction(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Defense-in-depth backstop for stories whose own agent_instructions
    still say 'write failing tests first' (pre-dating a test-author-phase
    story split): the trailing steering block must explicitly tell the
    executor to disregard that instruction, not just describe that tests
    exist."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "disregard", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "TDD - write failing tests FIRST, then implement.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: True)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9600)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("disregard", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert "DISREGARD" in task
    assert p._NEVER_TOUCH_TESTS_STEERING in task


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

    # Without mode — must succeed. (backend_name reflects the real registry's
    # roles.planner.provider - ollama/glm while Claude usage is capped; the
    # point of this assertion is just "the call succeeded", not the specific
    # value.)
    backend_name, _model = p._resolve_planner_backend("ollama", "gpt-oss:20b")
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


# ---------- get_role_config shows planner resolving to the registry default ----------

def test_get_role_config_planner_resolves_to_registry_planner_entry(monkeypatch):
    """get_role_config(plan_name=None) must show the planner role resolving
    to the registry's roles.planner entry - ollama/glm (tag glm-5.2:cloud)
    while Claude usage is capped (see
    test_resolve_planner_backend_defaults_to_registry_planner_entry_no_env_no_role_config)."""
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

    source = inspect.getsource(p)
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