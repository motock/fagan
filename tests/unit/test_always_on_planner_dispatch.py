"""Tests for the always-on guided-decomposition planner refactor (backend
resolution, always-on dispatch, tests_already_authored / authored_test_files
grounding).

Split out of test_always_on_planner.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._always_on_planner_helpers.
"""
from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
    role_registry,
)
from pipeline import server as p
from pipeline import ticketing as pt
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

# ---------- (a) registry default: ollama/glm, no env, no role_config ----------

def test_resolve_planner_backend_defaults_to_registry_planner_entry_no_env_no_role_config(monkeypatch):
    """With NO env vars and NO plan_role_config, the planner must resolve to
    whatever roles.planner says in the registry - a precedence test against
    a synthetic registry, not a lock on any specific provider/model, so
    swapping the live model_registry.json's planner entry never breaks this
    test. This is the always-on default - no PIPELINE_DECOMPOSE mode flag
    involved."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_PLANNER_MODEL", raising=False)
    fake_registry = {
        "providers": {"acme": {"models": {"widget": {"tag": "widget-v1"}}}},
        "roles": {"planner": {"provider": "acme", "model": "widget"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: fake_registry)
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "acme"
    assert model == "widget-v1"


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


