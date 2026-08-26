"""Tests for the pipeline MCP server: test-runner detection, acceptance scoping, build-command detection, persona helpers, and _invoke_overlord role_registry consultation.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess
from pathlib import Path

import pytest

from app import (
    role_registry,
)
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _story,
    agents_dir,
    plan_dir,
)

# ---------- Fixtures ----------




# ---------- Test runner detection ----------
def test_detect_test_command_finds_root_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == ["npm", "test"]


def test_detect_test_command_falls_back_to_subdirectory(tmp_path):
    # Project lives in a subdirectory (e.g. engine/) rather than repo root.
    sub = tmp_path / "engine"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == sub
    assert cmd == ["npm", "test"]


def test_detect_test_command_prefers_root_over_subdirectory(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    sub = tmp_path / "engine"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    test_dir, _cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path


def test_detect_test_command_no_marker_anywhere_falls_back_to_npm_test(tmp_path):
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == ["npm", "test"]


def test_detect_test_command_pyproject_uses_venv_python_when_present(tmp_path):
    # A bare `pytest` on PATH may be a different interpreter than the project
    # venv (e.g. homebrew python missing fastapi) and false-fail the test gate.
    # When a project venv sits next to pyproject.toml, grade with it.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == [
        str(tmp_path / ".venv" / "bin" / "python"), "-m", "pytest",
        "--override-ini=testpaths=.", "--ignore=tests/benchmark", "--ignore=tests/experiments",
    ]


def test_detect_test_command_pyproject_falls_back_to_bare_pytest_without_venv(tmp_path):
    # No venv and not inside a git worktree -> bare `pytest` (existing behavior).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    test_dir, cmd = p.detect_test_command(tmp_path)
    assert test_dir == tmp_path
    assert cmd == [
        "pytest", "--override-ini=testpaths=.", "--ignore=tests/benchmark", "--ignore=tests/experiments",
    ]


def test_detect_test_command_worktree_uses_main_repo_venv_via_git_common_dir(tmp_path):
    # The real Mode 5 scenario: the agent ran in a git worktree, which does NOT
    # contain .venv (it is gitignored). A bare `pytest` there resolves to the
    # PATH interpreter and false-fails. The detector must follow the worktree's
    # git link (`git rev-parse --git-common-dir` -> main repo .git) to find the
    # main repo's .venv and grade with that interpreter.
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*a: str) -> None:
        subprocess.run(
            ["git", *a], cwd=repo, check=True, capture_output=True, text=True)

    _git("init")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "test")
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    _git("add", "-A")
    _git("commit", "-m", "init")
    # Main-repo venv (untracked -- not present in the worktree checkout).
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "wt-branch"],
        cwd=repo, check=True, capture_output=True, text=True)
    # Sanity: the worktree has pyproject.toml but NOT .venv.
    assert (worktree / "pyproject.toml").exists()
    assert not (worktree / ".venv").exists()
    test_dir, cmd = p.detect_test_command(worktree)
    assert test_dir == worktree
    assert cmd == [
        str(repo / ".venv" / "bin" / "python"), "-m", "pytest",
        "--override-ini=testpaths=.", "--ignore=tests/benchmark", "--ignore=tests/experiments",
    ]


def test_detect_test_command_override_does_not_exclude_tests_unit(tmp_path):
    # Regression guard for the real bug: this repo's own reorg moved every
    # real test file under tests/unit/, so a blanket `--ignore=tests` (which
    # excludes the whole tests/ directory, tests/unit included) makes the
    # override collect ZERO tests ("no tests ran", exit code 5) - silently
    # treated as a full-suite failure by the rework done-bar
    # (scripts/local_agent_oracle.py:_full_suite_result) even though nothing
    # is actually broken. The override must only exclude the benchmark/
    # experiment harness directories, matching .github/workflows/ci.yml's
    # own `--ignore=tests/benchmark --ignore=tests/experiments`.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    unit_dir = tmp_path / "tests" / "unit"
    unit_dir.mkdir(parents=True)
    (unit_dir / "test_sample.py").write_text("def test_ok():\n    assert True\n")

    test_dir, cmd = p.detect_test_command(tmp_path)
    result = subprocess.run(
        cmd, cwd=test_dir, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


# ---------- _scope_test_cmd_to_acceptance (FM-A non-pytest scoping) ----------
def test_scope_pytest_appends_acceptance_paths():
    scoped = p._scope_test_cmd_to_acceptance(
        ["pytest"], ["tests/acceptance.py"], Path("/w"))
    assert scoped == ["pytest", "tests/acceptance.py"]


def test_scope_pytest_venv_form_appends_acceptance_paths():
    scoped = p._scope_test_cmd_to_acceptance(
        ["/w/.venv/bin/python", "-m", "pytest"], ["tests/acceptance.py"], Path("/w"))
    assert scoped == ["/w/.venv/bin/python", "-m", "pytest", "tests/acceptance.py"]


def test_scope_cargo_uses_test_stem_to_exclude_implementers_own_test():
    # The acceptance fixture tests/test_acceptance.rs becomes --test
    # test_acceptance, which runs ONLY that integration test — NOT the
    # implementer's own tests/test_lru_cache.rs (FM-A: interval_merge_js was
    # rejected because unscoped `npm test` ran the implementer's buggy test).
    scoped = p._scope_test_cmd_to_acceptance(
        ["cargo", "test"], ["/w/tests/test_acceptance.rs"], Path("/w"))
    assert scoped == ["cargo", "test", "--test", "test_acceptance"]


def test_scope_cargo_multiple_acceptance_fixtures():
    scoped = p._scope_test_cmd_to_acceptance(
        ["cargo", "test"],
        ["/w/tests/test_acceptance.rs", "/w/tests/test_oracle2.rs"], Path("/w"))
    assert scoped == ["cargo", "test", "--test", "test_acceptance",
                      "--test", "test_oracle2"]


def test_scope_cargo_falls_back_when_acceptance_not_under_tests_dir():
    # A unit-test acceptance file (src/...) can't be named via --test; fall
    # back to None so the caller runs the full suite (no regression).
    assert p._scope_test_cmd_to_acceptance(
        ["cargo", "test"], ["/w/src/acceptance.rs"], Path("/w")) is None


def test_scope_npm_node_test_uses_explicit_paths(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test test/*.test.js"}}))
    scoped = p._scope_test_cmd_to_acceptance(
        ["npm", "test"], [str(tmp_path / "test" / "acceptance.test.js")], tmp_path)
    assert scoped == ["node", "--test", str(tmp_path / "test" / "acceptance.test.js")]


def test_scope_npm_falls_back_for_jest_script(tmp_path):
    # jest can't be safely scoped without knowing its -t filter syntax; fall
    # back to None (full suite) so we never run a nonsense command.
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}}))
    assert p._scope_test_cmd_to_acceptance(
        ["npm", "test"], [str(tmp_path / "test" / "acceptance.test.js")], tmp_path) is None


def test_scope_returns_none_when_no_acceptance_paths():
    assert p._scope_test_cmd_to_acceptance(["pytest"], [], Path("/w")) is None
    assert p._scope_test_cmd_to_acceptance(["cargo", "test"], [], Path("/w")) is None


def test_scope_returns_none_for_unrecognized_runner():
    # mvn/gradle/make: no safe scoping -> None (full suite).
    assert p._scope_test_cmd_to_acceptance(
        ["mvn", "test"], ["acceptance.py"], Path("/w")) is None
    assert p._scope_test_cmd_to_acceptance(
        ["make", "test"], ["acceptance.py"], Path("/w")) is None


# ---------- detect_build_command (T4) ----------
def test_detect_build_command_finds_npm_build_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    result = p.detect_build_command(tmp_path)
    assert result == (tmp_path, ["npm", "run", "build"])


def test_detect_build_command_prefers_yarn_when_lockfile_present(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    (tmp_path / "yarn.lock").write_text("")
    result = p.detect_build_command(tmp_path)
    assert result == (tmp_path, ["yarn", "build"])


def test_detect_build_command_none_when_package_json_has_no_build_script(tmp_path):
    # A package.json without a "build" script (e.g. a library with no bundling
    # step) must not be treated as having a build gate to enforce.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    assert p.detect_build_command(tmp_path) is None


def test_detect_build_command_finds_cargo_build(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    result = p.detect_build_command(tmp_path)
    assert result == (tmp_path, ["cargo", "build"])


def test_detect_build_command_falls_back_to_subdirectory(tmp_path):
    sub = tmp_path / "web"
    sub.mkdir()
    (sub / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    result = p.detect_build_command(tmp_path)
    assert result == (sub, ["npm", "run", "build"])


def test_detect_build_command_none_when_no_marker_anywhere(tmp_path):
    assert p.detect_build_command(tmp_path) is None


# ---------- Persona helpers ----------
def test_persona_body_strips_frontmatter(agents_dir):
    body = p._persona_body("software-engineer")
    assert "Engineer body." in body
    assert "name:" not in body
    assert not body.startswith("---")


def test_persona_default_model_reads_frontmatter(agents_dir):
    assert p._persona_default_model("software-engineer") == "sonnet"
    assert p._persona_default_model("overlord") == "opus"


def test_persona_body_unknown_raises(agents_dir):
    with pytest.raises(FileNotFoundError):
        p._persona_body("does-not-exist")


def test_persona_default_model_unknown_returns_none(agents_dir):
    assert p._persona_default_model("does-not-exist") is None


# ---------- _invoke_overlord consults role_registry ----------
def test_invoke_overlord_defaults_to_claude_persona_model_when_unconfigured(
    agents_dir, monkeypatch,
):
    """Zero-config regression guard: with no registry entry, plan_role_config,
    or PIPELINE_BACKEND_OVERLORD override, overlord must resolve exactly as
    before - claude, persona-declared model ("opus" per agents_dir's
    overlord.md). The registry is forced empty so this stays a true
    zero-config test even when model_registry.json declares an overlord
    entry (which makes the default-configured path resolve elsewhere)."""
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)
    # Force an empty registry (no overlord role) so the fallback path is
    # exercised regardless of what model_registry.json currently declares.
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "ok"

    def _fake_get_backend(role, name=None):
        captured["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._invoke_overlord("a question")

    assert captured["name"] == "claude"
    assert captured["model"] == "opus"


def test_invoke_overlord_provider_from_registry(agents_dir, monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)
    registry = {
        "providers": {"ollama": {"models": {"devstral": {"tag": "devstral:24b"}}}},
        "roles": {"overlord": {"provider": "ollama", "model": "devstral"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, *, model, **kwargs):
            captured["model"] = model
            return "ok"

    def _fake_get_backend(role, name=None):
        captured["name"] = name
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._invoke_overlord("a question")

    assert captured["name"] == "ollama"
    assert captured["model"] == "devstral:24b"


def test_invoke_overlord_plan_role_config_beats_registry(agents_dir, monkeypatch):
    registry = {
        "providers": {"ollama": {"models": {}}, "claude": {"models": {}}},
        "roles": {"overlord": {"provider": "ollama"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    captured = {}

    def _fake_get_backend(role, name=None):
        captured["name"] = name

        class _FakeDriver:
            def complete(self, prompt, *, model, **kwargs):
                return "ok"
        return _FakeDriver()

    monkeypatch.setattr(p.backend, "get_backend", _fake_get_backend)

    p._invoke_overlord(
        "a question", plan_role_config={"overlord": {"provider": "claude"}},
    )

    assert captured["name"] == "claude"


# ---------- request_decision / list_decisions ----------
def test_request_decision_passes_plan_role_config_from_manifest_to_overlord(
    plan_dir, agents_dir, monkeypatch,
):
    (plan_dir / "rdcfg.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {},
        "role_config": {"overlord": {"provider": "mlx"}},
    }))
    captured = {}

    def _fake_invoke(prompt, plan_role_config=None):
        captured["plan_role_config"] = plan_role_config
        return "RULING: x\nTIER: routine\nRISK: low\nRATIONALE: y\nNOTIFY_USER: no\n"

    monkeypatch.setattr(p, "_invoke_overlord", _fake_invoke)

    p.request_decision("rdcfg", "S1", "q", ["a", "b"])

    assert captured["plan_role_config"] == {"overlord": {"provider": "mlx"}}


def test_request_decision_records_and_returns(plan_dir, agents_dir, monkeypatch):
    canned = (
        "RULING: Use the existing http client; do not add a new dependency.\n"
        "TIER: routine\n"
        "RISK: low\n"
        "RATIONALE: The stack already includes httpx; adding requests duplicates it.\n"
        "NOTIFY_USER: no\n"
    )
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: canned)

    result = p.request_decision(
        "myplan", "PIPE-7",
        "Should I add the requests library?",
        ["add requests", "use existing httpx"],
        context="parser story",
    )
    assert result["ruling"].startswith("Use the existing http client")
    assert result["tier"] == "routine"
    assert result["risk"] == "low"
    assert result["notify_user"] is False
    assert result["decided_by"] == "overlord"

    log = json.loads((plan_dir / "myplan.decisions.json").read_text())
    assert len(log) == 1
    assert log[0]["story_key"] == "PIPE-7"
    assert "decided_at" in log[0]


def test_request_decision_notify_and_high_risk_parsed(plan_dir, agents_dir, monkeypatch):
    canned = (
        "RULING: Hold for human review.\n"
        "TIER: park-and-ping\n"
        "RISK: high\n"
        "RATIONALE: Touches auth.\n"
        "NOTIFY_USER: yes\n"
    )
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: canned)
    result = p.request_decision("myplan", "PIPE-9", "q", ["a", "b"])
    assert result["notify_user"] is True
    assert result["risk"] == "high"
    assert result["tier"] == "park-and-ping"


def test_list_decisions_empty_then_populated(plan_dir, agents_dir, monkeypatch):
    assert p.list_decisions("emptyplan") == []
    monkeypatch.setattr(
        p, "_invoke_overlord",
        lambda prompt, **k: "RULING: x\nTIER: routine\nRISK: low\nRATIONALE: y\nNOTIFY_USER: no\n",
    )
    p.request_decision("myplan", "S1", "q", ["a"])
    p.request_decision("myplan", "S2", "q", ["a"])
    items = p.list_decisions("myplan")
    assert len(items) == 2
    assert {i["story_key"] for i in items} == {"S1", "S2"}


# ---------- _plan_role_config / get_role_config (discoverability) ----------
def test_plan_role_config_returns_empty_dict_when_manifest_missing(plan_dir):
    assert p._plan_role_config("does-not-exist") == {}


def test_plan_role_config_returns_empty_dict_when_manifest_has_no_role_config(
    plan_dir,
):
    (plan_dir / "noroles.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {}, "repo_root": "/tmp"})
    )
    assert p._plan_role_config("noroles") == {}


def test_plan_role_config_reads_role_config_block(plan_dir):
    (plan_dir / "withroles.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"review": {"provider": "mlx", "model": "qwen"}},
    }))
    assert p._plan_role_config("withroles") == {
        "review": {"provider": "mlx", "model": "qwen"},
    }


def test_plan_role_config_survives_malformed_manifest(plan_dir):
    (plan_dir / "broken.manifest.json").write_text("{not valid json")
    assert p._plan_role_config("broken") == {}


def test_get_role_config_reports_all_five_roles_with_no_config(agents_dir, monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_DECOMPOSE", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})

    result = p.get_role_config()

    assert result["ok"] is True
    assert set(result["roles"]) == {"overlord", "planner", "dispatch", "review", "decompose"}
    assert result["roles"]["overlord"]["provider"] == "claude"
    assert result["roles"]["review"]["provider"] == "claude"


def test_get_role_config_reflects_registry_override(agents_dir, monkeypatch):
    registry = {
        "providers": {"mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}}},
        "roles": {"review": {"provider": "mlx", "model": "qwen"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND_OVERLORD", raising=False)

    result = p.get_role_config()

    assert result["roles"]["review"] == {
        "provider": "mlx", "model": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
    }
    # Unrelated roles are unaffected by review's override.
    assert result["roles"]["overlord"]["provider"] == "claude"


def test_get_role_config_reflects_plan_role_config(plan_dir, agents_dir, monkeypatch):
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    (plan_dir / "cfgplan.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"overlord": {"provider": "ollama"}},
    }))

    result = p.get_role_config(plan_name="cfgplan")

    assert result["roles"]["overlord"]["provider"] == "ollama"


# ---------- Persona/model-aware dispatch ----------


def test_dispatch_command_uses_persona_body_and_model(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer", model="opus"), "PIPE-1")
    assert "Engineer body." in spec["system"]
    assert spec["model"] == "opus"


def test_dispatch_command_falls_back_to_persona_default_model(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer"), "PIPE-2")
    assert spec["model"] == "sonnet"


def test_dispatch_command_no_persona_uses_default_model_and_no_system_prompt(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-3")
    assert spec["model"] == p.DEFAULT_MODEL
    assert spec["system"] is None


def test_dispatch_command_unknown_persona_raises(agents_dir):
    with pytest.raises(FileNotFoundError):
        p._build_dispatch_command(_story(persona="no-such-persona"), "PIPE-4")


def test_dispatch_command_reviewer_tools_are_read_only(agents_dir):
    spec = p._build_dispatch_command(_story(persona="code-reviewer"), "PIPE-5")
    assert spec["allowed_tools"] == "Bash,Read"


def test_software_engineer_preexisting_failure_guidance():
    content = (Path(__file__).parent.parent.parent / "agents" / "software-engineer.md").read_text()
    assert "pre-existing" in content
    assert "out of scope" in content


def test_dispatch_command_resume_includes_completed_steps_and_hint(agents_dir):
    journal = [
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]
    spec = p._build_dispatch_command(_story(), "PIPE-1", resume_journal=journal)
    prompt = spec["prompt"]
    assert "RESUMING" in prompt
    assert "Wrote the parser" in prompt
    assert "add validation" in prompt
    assert "do not redo" in prompt.lower()


def test_dispatch_command_resume_falls_back_to_test_run_directive_when_no_hint(agents_dir):
    journal = [
        {"step": "s", "summary": "did stuff",
         "next_hint": "", "commit": "sha", "ts": "x"},
    ]
    spec = p._build_dispatch_command(_story(), "PIPE-1", resume_journal=journal)
    prompt = spec["prompt"]
    assert "Review the worktree state and continue." not in prompt
    assert ("pytest" in prompt.lower() or "test suite" in prompt.lower())


def test_dispatch_command_no_resume_journal_uses_original_prompt(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1")
    prompt = spec["prompt"]
    assert "RESUMING" not in prompt
    assert "completing issue" in prompt


def test_dispatch_command_includes_checkpoint_instruction_when_plan_name_given(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1", plan_name="myplan")
    prompt = spec["prompt"]
    assert "checkpoint" in prompt.lower()
    assert "myplan" in prompt
    assert "PIPE-1" in prompt


def test_dispatch_command_omits_checkpoint_instruction_without_plan_name(agents_dir):
    spec = p._build_dispatch_command(_story(), "PIPE-1")
    assert "checkpoint tool" not in spec["prompt"].lower()


def test_dispatch_command_resume_also_includes_checkpoint_instruction(agents_dir):
    journal = [
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]
    spec = p._build_dispatch_command(
        _story(), "PIPE-1", plan_name="myplan", resume_journal=journal,
    )
    prompt = spec["prompt"]
    assert "checkpoint" in prompt.lower()
    assert "myplan" in prompt


def test_dispatch_command_default_tools(agents_dir):
    spec = p._build_dispatch_command(_story(persona="software-engineer"), "PIPE-6")
    assert spec["allowed_tools"] == "Bash,Edit,Write,Read"


def test_dispatch_command_includes_review_feedback(agents_dir):
    # A redispatched changes_requested story must carry the reviewer's feedback
    # into the prompt so the agent knows what to fix.
    feedback = "The error path is untested and the SQL is injectable."
    spec = p._build_dispatch_command(_story(), "PIPE-1", review_feedback=feedback)
    assert feedback in spec["prompt"]
    assert "REQUESTED CHANGES" in spec["prompt"].upper()


