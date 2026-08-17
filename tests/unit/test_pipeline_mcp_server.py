"""Tests for the pipeline MCP server.

Run with the project venv:
    cd ~/.claude/mcp-servers/pipeline && .venv/bin/python -m pytest -q

External boundaries (the `claude` subprocess, git, gh, Plane HTTP) are mocked;
internal logic is exercised directly. Tools are plain callables after the
@mcp.tool() decorator, so they are imported and called as functions.
"""

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
    role_registry,
)
from pipeline import checkpoint as pcheckpoint
from pipeline import ci as pci
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import planner as pplanner
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage


# ---------- Fixtures ----------
@pytest.fixture(autouse=True)
def _clear_caches():
    pt._state_cache.clear()
    pt._label_cache.clear()
    # Reset the process-level Plane reachability verdict introduced in
    # test_ticketing_reachability.py's change: a prior test that found Plane
    # unreachable flips pt._plane_reachable to False, which would make every
    # later _plane_set_state call short-circuit and break retry/notify tests.
    pt._plane_reachable = None
    yield


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    """Default the test world to "Plane is wired up", which is what the
    existing tests assume (they mock plane_request and expect calls to
    happen). The Plane-optional path is exercised by the handful of tests
    that explicitly clear these to "" via _plane_disabled."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


@pytest.fixture
def _plane_disabled(monkeypatch):
    """Simulate an unconfigured Plane (no API key / workspace / project), so
    Plane calls must be skipped rather than fired at a dead endpoint."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
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
    # pipeline_persona imports AGENTS_DIR from pipeline_paths at module load
    # and reads it as a free var, so patches must land on its own binding too.
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
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
def _isolate_usage_state(tmp_path, monkeypatch):
    # Point the usage gate at a non-existent tmp file for EVERY test so none of
    # them read the developer's live ~/.claude/usage_state.json. That file is
    # rewritten every ~60s by the real usage poller, so advance_pipeline tests
    # that don't otherwise stub the gate were flaky - passing or failing purely
    # on whether the live session/week usage happened to be over the pause
    # threshold when the suite ran. A missing file reads as "not paused".
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture
def usage_state_path(_isolate_usage_state):
    # Same isolated path as the autouse fixture; tests that want a specific gate
    # state write to it.
    return _isolate_usage_state


SAMPLE_USAGE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 9% used · resets Jun 18 at 11:59am (America/Chicago)\n"
    "Current week (all models): 48% used · resets Jun 23 at 9am (America/Chicago)\n\n"
    "What's contributing to your limits usage?\n"
)


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
def _story(**over):
    base = {"summary": "Do the thing", "agent_instructions": "Build it with tests."}
    base.update(over)
    return base


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


# ---------- Plan schema carry-through ----------
def test_save_plan_preserves_persona_model_risk(plan_dir):
    plan = {
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    p.save_plan("carry", json.dumps(plan))
    saved = json.loads((plan_dir / "carry.json").read_text())
    story = saved["epics"][0]["stories"][0]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"


def _fake_plane(method, path, **kwargs):
    if path.endswith("/states/"):
        return {"results": [
            {"group": "backlog", "id": "st-backlog"},
            {"group": "started", "id": "st-started"},
            {"group": "completed", "id": "st-done"},
        ]}
    if path.endswith("/labels/") and method == "GET":
        return {"results": []}
    if path.endswith("/labels/") and method == "POST":
        return {"id": "label-1"}
    if path.endswith("/epics/") and method == "POST":
        return {"id": "epic-1"}
    if path.endswith("/work-items/") and method == "POST":
        return {"id": "issue-1"}
    return {}


def test_ingest_plan_carries_persona_model_risk_into_manifest(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    (plan_dir / "ing.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing.manifest.json").read_text())
    story = manifest["stories"]["issue-1"]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"


def test_ingest_plan_carries_backend_into_manifest(plan_dir, monkeypatch, tmp_path):
    """A plan can pin a story's dispatch provider upfront (e.g. "mlx"), not
    just via a runtime escalation flip - _LOCAL_BACKEND_NAMES already
    includes ollama/lmstudio/mlx, so this is purely a plan-authoring gap."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="mlx")]}],
    }
    (plan_dir / "ing2.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing2")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing2.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "mlx"


def test_ingest_plan_backend_defaults_to_none_when_omitted(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story()]}],
    }
    (plan_dir / "ing3.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing3")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing3.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] is None


def test_ingest_plan_rejects_unknown_backend_value(plan_dir, monkeypatch, tmp_path):
    """Fail closed on a typo'd backend name at ingest time rather than
    letting it reach dispatch_story and raise NotImplementedError deep
    inside get_backend."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="some-typo")]}],
    }
    (plan_dir / "ing4.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing4")
    assert result["ok"] is False
    assert "some-typo" in result["error"]
    assert not (plan_dir / "ing4.manifest.json").exists()


def test_ingest_plan_accepts_auto_backend_value(plan_dir, monkeypatch, tmp_path):
    """"auto" is a valid story["backend"] value (resolved by
    _route_dispatch_backend before reaching get_backend), even though it's
    not a registered driver in backend._DRIVERS."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="auto")]}],
    }
    (plan_dir / "ing5.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing5")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing5.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "auto"


def test_ingest_plan_reingest_refreshes_backend_field(plan_dir, monkeypatch, tmp_path):
    """"backend" must be included in _INGEST_AUTHORED_STORY_FIELDS so a
    re-ingest updates it, like persona/model/risk already do."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1", backend="ollama")]}],
    }
    (plan_dir / "ing6.json").write_text(json.dumps(plan))
    p.ingest_plan("ing6")

    plan["epics"][0]["stories"] = [_story(key="S1", backend="mlx")]
    (plan_dir / "ing6.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing6")

    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing6.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "mlx"


def test_ingest_plan_remaps_local_keys_to_issue_ids_in_dependencies(plan_dir, monkeypatch, tmp_path):
    issue_ids = iter(["issue-1", "issue-2", "issue-3"])
    monkeypatch.setattr(pt, "plane_request",
        lambda method, path, **kw: (
            _fake_plane(method, path, **kw) if not path.endswith("/work-items/")
            else {"id": next(issue_ids)}
        ),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
                _story(key="S3", dependencies=["S1", "S2"]),
            ],
        }]
    }
    (plan_dir / "deps.json").write_text(json.dumps(plan))
    result = p.ingest_plan("deps")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "deps.manifest.json").read_text())
    stories = manifest["stories"]
    assert stories["issue-1"]["dependencies"] == []
    assert stories["issue-2"]["dependencies"] == ["issue-1"]
    assert stories["issue-3"]["dependencies"] == ["issue-1", "issue-2"]


def test_ingest_plan_leaves_unresolvable_dependency_keys_unchanged(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(key="S1", dependencies=["no-such-key"])],
        }]
    }
    (plan_dir / "dangling.json").write_text(json.dumps(plan))
    result = p.ingest_plan("dangling")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "dangling.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["dependencies"] == ["no-such-key"]


def test_ingest_plan_warns_on_isolation_only_acceptance_fixture(plan_dir, monkeypatch, tmp_path):
    """Non-blocking authoring nudge: a story whose instructions require
    wiring at a call site but whose acceptance fixture only invokes the unit
    directly should notify the user, without failing ingest. Root-caused
    live 2026-07-28 on harness-targeted-done-nudge -- see
    pipeline.build_detect._isolation_only_acceptance_warning."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(
                summary="wire the nudge at the call site",
                agent_instructions="Update the call site to pass content.",
                acceptance=[{
                    "path": "tests/test_nudge.py",
                    "source": "def test_x():\n    assert _no_tool_nudge(0)\n",
                }],
            )],
        }]
    }
    (plan_dir / "isowarn.json").write_text(json.dumps(plan))
    result = p.ingest_plan("isowarn")
    assert result["ok"] is True
    notifications = (plan_dir / "isowarn.notifications.log").read_text()
    assert "isolation-only" in notifications
    assert "wire the nudge at the call site" in notifications


def test_ingest_plan_no_warning_when_fixture_exercises_integration(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(
                summary="wire the nudge at the call site",
                agent_instructions="Update the call site to pass content.",
                acceptance=[{
                    "path": "tests/test_nudge.py",
                    "source": "def test_x(monkeypatch):\n    rc = main()\n    assert rc == 0\n",
                }],
            )],
        }]
    }
    (plan_dir / "isook.json").write_text(json.dumps(plan))
    result = p.ingest_plan("isook")
    assert result["ok"] is True
    assert not (plan_dir / "isook.notifications.log").exists()


# ---------- Logging hygiene ----------
def test_http_loggers_are_quieted():
    """Importing the server caps httpx/httpcore at WARNING so the per-tick
    HTTP probes don't flood the unattended launchd logs at INFO."""
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ---------- Plane optional (unconfigured) ----------
def test_plane_enabled_reflects_config(monkeypatch):
    assert p._plane_enabled() is True  # set by _plane_configured fixture
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    assert p._plane_enabled() is False


def _explode_plane(*a, **kw):
    raise AssertionError("plane_request must not be called when Plane is unconfigured")


def test_ingest_plan_without_plane_skips_calls_and_keys_by_story_key(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
            ],
        }]
    }
    (plan_dir / "noplane.json").write_text(json.dumps(plan))
    result = p.ingest_plan("noplane")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "noplane.manifest.json").read_text())
    # Stories are keyed by their plan key (no Plane UUID to key on), and
    # dependencies still resolve to those keys.
    assert set(manifest["stories"]) == {"S1", "S2"}
    assert manifest["stories"]["S2"]["dependencies"] == ["S1"]


def test_ingest_plan_without_plane_synthesizes_keys_when_absent(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(), _story()]}],
    }
    (plan_dir / "nokeys.json").write_text(json.dumps(plan))
    result = p.ingest_plan("nokeys")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "nokeys.manifest.json").read_text())
    assert len(manifest["stories"]) == 2  # two distinct synthetic keys


def test_ingest_plan_survives_plane_configured_but_unreachable(
    plan_dir, monkeypatch, tmp_path, capsys,
):
    # Plane IS configured here (the autouse _plane_configured fixture), so
    # get_ticket_provider() resolves to PlaneTicketProvider - but every call
    # fails at the connection level (host down, timeout, ...). ingest_plan
    # must still succeed by falling back to synthesized/local story keys,
    # exactly like the "Plane unconfigured" path does.
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(key="S1"), _story(key="S2", dependencies=["S1"])],
        }]
    }
    (plan_dir / "unreachable.json").write_text(json.dumps(plan))
    result = p.ingest_plan("unreachable")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "unreachable.manifest.json").read_text())
    assert set(manifest["stories"]) == {"S1", "S2"}
    assert manifest["stories"]["S2"]["dependencies"] == ["S1"]
    # The epic itself failed to create too, so it must not appear.
    assert manifest["epics"] == {}
    # The failure must be surfaced, not silently swallowed forever.
    assert "Warning" in capsys.readouterr().out


def test_plane_set_state_noop_when_plane_disabled(_plane_disabled, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    assert p._plane_set_state("S1", "started") is True


def test_mark_story_done_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    (plan_dir / "md.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "pr_open"}}}))
    result = p.mark_story_done("md", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "md.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "done"


def test_mark_story_in_progress_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    (plan_dir / "mip.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "todo"}}}))
    result = p.mark_story_in_progress("mip", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "mip.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


# ---------- TicketProvider abstraction ----------
# Plane is already optional (see tests above); these cover the pluggable
# TicketProvider seam: selection via PIPELINE_TICKET_PROVIDER, the Null/Plane
# providers, and the documented Jira stub.
def test_get_ticket_provider_auto_resolves_to_plane_when_configured(monkeypatch):
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.PlaneTicketProvider)
    assert provider.enabled is True


def test_get_ticket_provider_auto_resolves_to_null_when_unconfigured(
    _plane_disabled, monkeypatch,
):
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.NullTicketProvider)
    assert provider.enabled is False


def test_get_ticket_provider_none_forces_null_even_when_plane_configured(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.NullTicketProvider)


def test_get_ticket_provider_plane_forced_without_config_raises(
    _plane_disabled, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "plane")
    with pytest.raises(ValueError, match="PLANE_"):
        p.get_ticket_provider()


def test_get_ticket_provider_jira_returns_stub(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "jira")
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.JiraTicketProvider)


def test_get_ticket_provider_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        p.get_ticket_provider()


def test_null_ticket_provider_every_op_is_a_noop():
    provider = p.NullTicketProvider()
    assert provider.enabled is False
    assert provider.create_epic("E1") is None
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None
    assert provider.set_state("S1", p.LogicalState.DONE) is True
    assert provider.resolve_key("S1") == "S1"


def test_plane_ticket_provider_create_epic_and_story_delegate_to_plane_request(
    monkeypatch,
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    provider = p.PlaneTicketProvider()
    assert provider.enabled is True
    epic_id = provider.create_epic("E1")
    assert epic_id == "epic-1"
    issue_id = provider.create_story("S1", "desc", epic_id, "agent-pipeline")
    assert issue_id == "issue-1"


def test_plane_ticket_provider_create_epic_falls_back_to_none_on_api_error(
    monkeypatch,
):
    # Epics are an optional Plane module; some instances don't expose it.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("404")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_epic("E1") is None


def test_plane_ticket_provider_create_epic_falls_back_to_none_on_connection_error(
    monkeypatch,
):
    # A connection failure (Plane host unreachable, timeout, ...) is not a
    # RuntimeError like a non-2xx response - it must be caught too, not just
    # the "epics module unsupported" 404 case.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_epic("E1") is None


def test_plane_ticket_provider_create_story_falls_back_to_none_on_connection_error(
    monkeypatch,
):
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None


def test_plane_ticket_provider_create_story_falls_back_to_none_on_api_error(
    monkeypatch,
):
    # create_story must tolerate a non-2xx RuntimeError the same way
    # create_epic already does, not just connection-level failures.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None


def test_plane_ticket_provider_create_story_returns_issue_id_when_epic_link_fails(
    monkeypatch, capsys,
):
    # The issue itself was created successfully; only the (optional) epic-
    # link call failed afterwards. Discarding issue_id here would orphan a
    # real Plane ticket (never referenced by the manifest) and cause
    # ingest_plan to create a duplicate issue on a retried ingest - the link
    # failure must degrade the link only, matching create_epic's "epic
    # support is optional" contract, not discard a real created id.
    def flaky_plane(method, path, **kwargs):
        if "/epics/" in path and path.endswith("/issues/"):
            raise httpx.ConnectError("connection refused")
        return _fake_plane(method, path, **kwargs)

    monkeypatch.setattr(pt, "plane_request", flaky_plane)
    provider = p.PlaneTicketProvider()
    issue_id = provider.create_story("S1", "desc", "epic-1", "agent-pipeline")
    assert issue_id == "issue-1"
    assert "Warning" in capsys.readouterr().out


def test_plane_ticket_provider_set_state_delegates_to_plane_set_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pt, "_plane_set_state",
        lambda key, group, plan_name=None: calls.append((key, group, plan_name)) or True,
    )
    provider = p.PlaneTicketProvider()
    assert provider.set_state("S1", p.LogicalState.IN_PROGRESS, "myplan") is True
    assert calls == [("S1", "started", "myplan")]


def test_plane_ticket_provider_resolve_key_delegates_to_resolve_issue_uuid(monkeypatch):
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: f"resolved-{key}")
    provider = p.PlaneTicketProvider()
    assert provider.resolve_key("PIPE-7") == "resolved-PIPE-7"


def test_jira_ticket_provider_every_op_raises_not_implemented():
    provider = p.JiraTicketProvider()
    with pytest.raises(NotImplementedError):
        provider.create_epic("E1")
    with pytest.raises(NotImplementedError):
        provider.create_story("S1", "desc", None, "agent-pipeline")
    with pytest.raises(NotImplementedError):
        provider.set_state("S1", p.LogicalState.DONE)
    with pytest.raises(NotImplementedError):
        provider.resolve_key("S1")


def test_mark_story_in_progress_routes_through_ticket_provider(plan_dir, monkeypatch):
    calls = []

    class _FakeProvider:
        def set_state(self, story_key, state, plan_name=None):
            calls.append((story_key, state, plan_name))
            return True

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _FakeProvider())
    (plan_dir / "tpmip.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "todo"}}}))
    result = p.mark_story_in_progress("tpmip", "S1")
    assert result["ok"] is True
    assert calls == [("S1", p.LogicalState.IN_PROGRESS, "tpmip")]


def test_mark_story_done_routes_through_ticket_provider(plan_dir, monkeypatch):
    calls = []

    class _FakeProvider:
        def set_state(self, story_key, state, plan_name=None):
            calls.append((story_key, state, plan_name))
            return True

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _FakeProvider())
    (plan_dir / "tpmd.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "pr_open"}}}))
    result = p.mark_story_done("tpmd", "S1")
    assert result["ok"] is True
    assert calls == [("S1", p.LogicalState.DONE, "tpmd")]


# ---------- mark_story_done plan-completion signal ----------
# When the last remaining non-done story is marked done, mark_story_done must
# signal that the whole plan is complete via a plan_completed key plus the full
# list of story keys. When any story is still not 'done' (including 'parked' or
# any other non-'done' terminal-looking state), the return dict must keep
# today's exact {'ok': True} shape - no plan_completed key at all - so existing
# equality assertions keep passing and callers can use .get('plan_completed')
# truthiness or `'plan_completed' in result` either way.

def test_mark_story_done_signals_plan_completed_when_all_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc1", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    })
    result = p.mark_story_done("pc1", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert set(result["stories"]) == {"S1", "S2"}
    manifest = _read_manifest(plan_dir, "pc1")
    assert manifest["stories"]["S2"]["status"] == "done"


def test_mark_story_done_omits_plan_completed_when_other_story_not_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc2", {
        "S1": {"status": "todo"},
        "S2": {"status": "in_progress"},
    })
    result = p.mark_story_done("pc2", "S1")
    assert result["ok"] is True
    # Exact today's shape: no plan_completed key at all.
    assert "plan_completed" not in result
    assert "stories" not in result
    assert result == {"ok": True}
    manifest = _read_manifest(plan_dir, "pc2")
    assert manifest["stories"]["S1"]["status"] == "done"
    assert manifest["stories"]["S2"]["status"] == "in_progress"


def test_mark_story_done_single_story_plan_completed(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc3", {
        "S1": {"status": "todo"},
    })
    result = p.mark_story_done("pc3", "S1")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert result["stories"] == ["S1"]
    manifest = _read_manifest(plan_dir, "pc3")
    assert manifest["stories"]["S1"]["status"] == "done"


def test_mark_story_done_parked_story_does_not_count_as_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc4", {
        "S1": {"status": "todo"},
        "S2": {"status": "parked", "parked_reason": "blocked"},
    })
    result = p.mark_story_done("pc4", "S1")
    assert result["ok"] is True
    # A 'parked' story is not 'done', so the plan is not complete.
    assert "plan_completed" not in result
    assert "stories" not in result
    assert result == {"ok": True}
    manifest = _read_manifest(plan_dir, "pc4")
    assert manifest["stories"]["S1"]["status"] == "done"
    assert manifest["stories"]["S2"]["status"] == "parked"


# ---------- mark_story_done retro PENDING.md tracking ----------
# When the last story of a plan whose top-level manifest repo_root equals this
# pipeline's own repo (PIPELINE_SELF_REPO_ROOT) completes, mark_story_done must
# append a line to RETRO_PENDING_PATH so a retrospective gets queued. Plans
# rooted in some other repo (external game/app plans dispatched through this
# pipeline) must NOT get a PENDING.md entry. The write must be idempotent and
# must only happen on the FINAL story (plan_completed), not every story.

@pytest.fixture
def retro_pending_path(tmp_path, monkeypatch):
    self_root = tmp_path / "self_repo"
    pending = self_root / "retros" / "PENDING.md"
    # raising=False so the fixture itself doesn't error before the
    # implementation adds these names; the test bodies then fail with
    # AssertionError on the missing behavior instead.
    monkeypatch.setattr(p, "PIPELINE_SELF_REPO_ROOT", self_root, raising=False)
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    return pending


def _write_manifest_with_repo_root(plan_dir, plan_name, stories, repo_root):
    """Like _write_manifest but adds a top-level repo_root key, which the
    retro-pending scoping rule keys off of."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": stories, "repo_root": repo_root}, indent=2
        )
    )


def test_mark_story_done_records_retro_pending_for_self_repo_plan(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    _write_manifest_with_repo_root(plan_dir, "rp1", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, str(self_root))
    result = p.mark_story_done("rp1", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    # PENDING.md must now exist and contain a line starting with the plan name.
    assert retro_pending_path.exists()
    content = retro_pending_path.read_text()
    lines = content.splitlines()
    matching = [ln for ln in lines if ln.startswith("- rp1 ")]
    assert len(matching) == 1
    # The line must carry the story count.
    assert "2 stories" in matching[0]


def test_mark_story_done_skips_retro_pending_for_non_self_repo_plan(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest_with_repo_root(plan_dir, "rp2", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, "/some/other/repo")
    result = p.mark_story_done("rp2", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    # External-repo plan: no PENDING.md entry at all.
    assert not retro_pending_path.exists()


def test_mark_story_done_skips_retro_pending_when_repo_root_absent(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    # No repo_root key at all -> not a self-repo plan.
    _write_manifest(plan_dir, "rp2b", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    })
    result = p.mark_story_done("rp2b", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert not retro_pending_path.exists()


def test_mark_story_done_retro_pending_is_idempotent(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    # Pre-create PENDING.md with an existing line for this plan.
    retro_pending_path.parent.mkdir(parents=True, exist_ok=True)
    retro_pending_path.write_text("- myplan \u2014 completed 2026-01-01, 1 stories\n")
    _write_manifest_with_repo_root(plan_dir, "myplan", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, str(self_root))
    result = p.mark_story_done("myplan", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    content = retro_pending_path.read_text()
    lines = content.splitlines()
    matching = [ln for ln in lines if ln.startswith("- myplan ")]
    # Exactly one line — no duplicate appended.
    assert len(matching) == 1
    assert matching[0] == "- myplan \u2014 completed 2026-01-01, 1 stories"


def test_mark_story_done_no_retro_pending_write_when_plan_not_yet_complete(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    _write_manifest_with_repo_root(plan_dir, "rp4", {
        "S1": {"status": "todo"},
        "S2": {"status": "todo"},
    }, str(self_root))
    # Complete only one of two stories — plan not yet complete.
    result = p.mark_story_done("rp4", "S1")
    assert result["ok"] is True
    assert "plan_completed" not in result
    assert result == {"ok": True}
    # Must NOT have written a PENDING.md entry on a non-final story.
    assert not retro_pending_path.exists()


def test_pipeline_self_repo_root_and_retro_pending_path_constants_exist():
    # The two new module-level constants must exist and be Path objects.
    assert hasattr(p, "PIPELINE_SELF_REPO_ROOT")
    assert hasattr(p, "RETRO_PENDING_PATH")
    from pathlib import Path as _Path
    assert isinstance(p.PIPELINE_SELF_REPO_ROOT, _Path)
    assert isinstance(p.RETRO_PENDING_PATH, _Path)
    # RETRO_PENDING_PATH must be PIPELINE_SELF_REPO_ROOT / "retros" / "PENDING.md".
    assert p.RETRO_PENDING_PATH == p.PIPELINE_SELF_REPO_ROOT / "retros" / "PENDING.md"
    # PIPELINE_SELF_REPO_ROOT must resolve to this repo's own root (server.py
    # lives at pipeline/server.py, so parent.parent is the repo root).
    assert p.PIPELINE_SELF_REPO_ROOT == _Path(p.__file__).resolve().parent.parent


def test_record_retro_pending_helper_writes_expected_line(tmp_path, monkeypatch):
    # The _record_retro_pending helper must exist and write a single line.
    self_root = tmp_path / "self_repo"
    pending = self_root / "retros" / "PENDING.md"
    monkeypatch.setattr(p, "PIPELINE_SELF_REPO_ROOT", self_root, raising=False)
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    assert hasattr(p, "_record_retro_pending")
    p._record_retro_pending("helperplan", 3)
    assert pending.exists()
    line = pending.read_text()
    assert line.startswith("- helperplan ")
    assert "3 stories" in line
    assert "completed" in line


# ---------- patch_story / set_story_status (T2) ----------
# These give a caller a sanctioned, lock-serialized way to edit a story's
# authored fields or transition its status, so nobody needs to hand-edit the
# manifest JSON directly - which races the 60s scheduler tick with no lock
# protecting the edit (2026-07-07 web-client-epic retro, §6).

def test_patch_story_updates_allowlisted_field_preserves_others(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps1", {
        "S1": {"summary": "Old summary", "agent_instructions": "Old.",
               "status": "todo", "dependencies": [], "model": "sonnet"},
    })
    result = p.patch_story("ps1", "S1", {"agent_instructions": "New, clarified."})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ps1")
    assert manifest["stories"]["S1"]["agent_instructions"] == "New, clarified."
    assert manifest["stories"]["S1"]["status"] == "todo"
    assert manifest["stories"]["S1"]["model"] == "sonnet"


def test_patch_story_can_update_tdd_split_opt_in(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "pstdd", {
        "S1": {"summary": "s", "status": "todo", "tdd_split": False},
    })
    result = p.patch_story("pstdd", "S1", {"tdd_split": True})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "pstdd")
    assert manifest["stories"]["S1"]["tdd_split"] is True


def test_patch_story_can_update_pr_url(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps2", {
        "S1": {"summary": "s", "status": "done"},
    })
    result = p.patch_story("ps2", "S1", {"pr_url": "https://example.com/pr/9"})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ps2")
    assert manifest["stories"]["S1"]["pr_url"] == "https://example.com/pr/9"


def test_patch_story_rejects_field_outside_allowlist(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps3", {
        "S1": {"summary": "s", "status": "todo"},
    })
    result = p.patch_story("ps3", "S1", {"status": "done"})
    assert result["ok"] is False
    assert "status" in result["error"]
    manifest = _read_manifest(plan_dir, "ps3")
    assert manifest["stories"]["S1"]["status"] == "todo"


def test_patch_story_rejects_missing_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps4", {})
    result = p.patch_story("ps4", "no-such-key", {"model": "opus"})
    assert result["ok"] is False
    assert "no-such-key" in result["error"]


def test_patch_story_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps5", {
        "S1": {"summary": "s", "status": "todo", "model": "sonnet"},
    })
    lock_path = plan_dir / "ps5.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.patch_story("ps5", "S1", {"model": "opus"})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "ps5")
    assert manifest["stories"]["S1"]["model"] == "sonnet"


def test_set_story_status_updates_to_valid_status(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss1", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss1", "S1", "interrupted")
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ss1")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_rejects_invalid_status(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss2", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss2", "S1", "definitely-not-a-status")
    assert result["ok"] is False
    assert "definitely-not-a-status" in result["error"]
    manifest = _read_manifest(plan_dir, "ss2")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_set_story_status_rejects_missing_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss3", {})
    result = p.set_story_status("ss3", "no-such-key", "todo")
    assert result["ok"] is False
    assert "no-such-key" in result["error"]


def test_set_story_status_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss4", {
        "S1": {"summary": "s", "status": "parked"},
    })
    lock_path = plan_dir / "ss4.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.set_story_status("ss4", "S1", "interrupted")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "ss4")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_ingest_plan_rejects_missing_repo_root(plan_dir, monkeypatch):
    """Without repo_root, advance_all_plans() falls back to the global
    REPO_ROOT for this plan - almost certainly the wrong repo (or a
    deliberately-broken sentinel, if one's configured to fail loudly rather
    than silently operate on the wrong repo). Catch it at ingest, not three
    silent merge-attempt failures later."""
    called = []
    monkeypatch.setattr(pt, "plane_request", lambda *a, **kw: called.append(1) or _fake_plane(*a, **kw))
    plan = {"epics": [{"summary": "E1", "stories": [_story()]}]}
    (plan_dir / "norepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("norepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "norepo.manifest.json").exists()
    assert not called  # must fail before any Plane side effects


def test_ingest_plan_rejects_nonexistent_repo_root_directory(plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": "/nonexistent-repo-root-set-per-plan-only",
        "epics": [{"summary": "E1", "stories": [_story()]}],
    }
    (plan_dir / "badrepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("badrepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "badrepo.manifest.json").exists()


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
class _NullSetStateProvider:
    """A no-op TicketProvider for mark_story_done completion-signal tests.

    Accepts set_state calls (so mark_story_done doesn't try to hit Plane) and
    returns True, mirroring the _FakeProvider pattern used by the existing
    ticket-provider routing tests.
    """

    def set_state(self, story_key, state, plan_name=None):
        return True


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


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
    assert "FAILED test_foo.py::test_bar" in result["review_feedback"]


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


def test_run_security_reviewer_uses_security_role_backend_not_review_env(agents_dir, monkeypatch):
    """T11/T12: the security-engineer pass resolves its OWN "security" role
    (registry default ollama/glm while Claude usage is capped) and must never
    silently follow PIPELINE_BACKEND_REVIEW=local the way ordinary review
    does - security review is routed by its own role, not the review env
    var. The stock registry security role is the always-on default here; a
    plan's role_config.security override is covered by
    test_run_security_reviewer_routes_via_plan_role_config_security."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
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
    assert captured["name"] == "ollama"


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
    role_config.security overrides the registry default, so a high-risk
    story can clear security review on a configured backend
    (e.g. ollama/glm) instead of dead-ending when Claude is unavailable.
    Unconfigured, it resolves to the registry's security role default
    (covered by
    test_run_security_reviewer_uses_security_role_backend_not_review_env)."""
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
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
    assert captured["model"] == "glm-5.2:cloud"


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
    PIPELINE_BACKEND_REVIEW=local exactly as before."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
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
    # model_registry.json now carries an explicit "review" entry
    # (claude/sonnet), so _run_reviewer passes the env-resolved provider
    # ("local", since PIPELINE_BACKEND_REVIEW wins) explicitly through to
    # get_backend instead of leaving it to get_backend's own internal
    # lookup - same real backend, just resolved one layer earlier now.
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


# ---------- Usage gate: new CLI format ----------

SAMPLE_USAGE_TEXT_NEW = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "What's contributing to your limits usage?\n"
    "Approximate, based on local sessions on this machine\n\n"
    "Last 24h · 1127 requests · 11 sessions\n"
    "  82% of your usage came from subagent-heavy sessions\n\n"
    "Last 7d · 7062 requests · 95 sessions\n"
    "  80% of your usage came from sessions active for 8+ hours\n"
)


def test_parse_usage_output_handles_new_request_count_format(monkeypatch):
    """New CLI format (request counts) is parsed into session_pct/week_pct."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 2000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT_NEW)
    # 1127/2000 = 56%, 7062/10000 = 70%
    assert result["session_pct"] == 56
    assert result["week_pct"] == 70


def test_parse_usage_output_new_format_clamps_to_100(monkeypatch):
    """Request count exceeding the threshold clamps to 100%, not above."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 500)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 1000)
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT_NEW)
    assert result["session_pct"] == 100
    assert result["week_pct"] == 100


def test_parse_usage_output_old_format_still_works():
    """Old percentage format continues to parse correctly after the update."""
    result = p._parse_usage_output(SAMPLE_USAGE_TEXT)
    assert result["session_pct"] == 9
    assert result["week_pct"] == 48


def test_parse_usage_output_raises_on_completely_unrecognized_format():
    with pytest.raises(ValueError):
        p._parse_usage_output("some text with no usage data at all")


def test_run_usage_probe_handles_new_cli_format(monkeypatch):
    """Usage probe works end-to-end with the new /cost JSON output format."""
    monkeypatch.setattr(pusage, "DAILY_REQUEST_THRESHOLD", 2000)
    monkeypatch.setattr(pusage, "WEEKLY_REQUEST_THRESHOLD", 10000)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT_NEW})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    result = p._run_usage_probe()
    assert result["session_pct"] == 56
    assert result["week_pct"] == 70
    assert "checked_at" in result


# ---------- Usage gate: bounded fail-closed ----------

def test_check_usage_pauses_after_prolonged_blindness(monkeypatch, usage_state_path):
    """After gate_blind persists beyond USAGE_BLIND_PAUSE_AFTER_SECONDS, set paused=True."""
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)   # always trigger stale path
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 100)

    old_enough = datetime.now(timezone.utc) - timedelta(seconds=200)
    prev = {
        "paused": False,
        "gate_blind": True,
        "blind_since": old_enough.isoformat(),
        "measured_at": old_enough.isoformat(),
        "checked_at": old_enough.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 50,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    result = p.check_usage()
    assert result["paused"] is True, "prolonged blindness should flip gate to paused (fail-closed)"


def test_check_usage_stays_open_during_short_blind_window(monkeypatch, usage_state_path):
    """A fresh blind window (within threshold) remains fail-open."""
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)   # always trigger stale path
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 3600)

    recent = datetime.now(timezone.utc) - timedelta(seconds=60)
    prev = {
        "paused": False,
        "gate_blind": True,
        "blind_since": recent.isoformat(),
        "measured_at": recent.isoformat(),
        "checked_at": recent.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 5,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    result = p.check_usage()
    assert result["paused"] is False, "short blind window should stay fail-open"


# ---------- Usage gate: log throttling ----------

def test_check_usage_blind_logs_only_on_transition_and_interval(monkeypatch, usage_state_path, capsys):
    """Blind stderr line is emitted only on first blindness + every USAGE_BLIND_LOG_INTERVAL polls."""
    monkeypatch.setattr(p, "USAGE_BLIND_LOG_INTERVAL", 10)
    monkeypatch.setattr(p, "USAGE_BLIND_PAUSE_AFTER_SECONDS", 9999)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 0)  # trigger blind path

    base_time = datetime.now(timezone.utc) - timedelta(seconds=100)
    prev = {
        "paused": False,
        "gate_blind": False,
        "measured_at": base_time.isoformat(),
        "checked_at": base_time.isoformat(),
        "session_pct": 0,
        "week_pct": 0,
        "consecutive_parse_failures": 0,
    }
    _write_usage_state_direct(usage_state_path, prev)

    monkeypatch.setattr(p, "_run_usage_probe", lambda: (_ for _ in ()).throw(ValueError("no parse")))

    # First call: first blind transition — should log.
    p.check_usage()
    out1 = capsys.readouterr().err
    assert out1 != "", "first blind transition should log"

    # Calls 2-9: not on the interval — should NOT log.
    for _ in range(8):
        p.check_usage()
    out2 = capsys.readouterr().err
    assert out2 == "", f"mid-interval blind polls should NOT log, got: {out2!r}"

    # Call 10: interval boundary — should log again.
    p.check_usage()
    out3 = capsys.readouterr().err
    assert out3 != "", "interval boundary should log"


# ---------- Path-traversal validation ----------

@pytest.mark.parametrize("bad_key", [
    "../etc/passwd",
    "../../secret",
    "a/b",
    "a\\b",
    "\x00null",
    "plan" + "/" + "story",
])
def test_validate_key_rejects_path_traversal(bad_key):
    with pytest.raises(ValueError, match="invalid"):
        p._validate_key(bad_key)


@pytest.mark.parametrize("good_key", [
    "my-plan",
    "PIPE-123",
    "story_abc",
    "abc123",
    "abc.def",
    "a" * 200,
])
def test_validate_key_allows_safe_names(good_key):
    p._validate_key(good_key)  # must not raise


def test_dispatch_story_rejects_traversal_plan_name(plan_dir, monkeypatch):
    with pytest.raises(ValueError, match="invalid"):
        p.dispatch_story("../evil", "story-1")


def test_dispatch_story_rejects_traversal_story_key(plan_dir, monkeypatch):
    with pytest.raises(ValueError, match="invalid"):
        p.dispatch_story("myplan", "../evil")


def test_check_story_status_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.check_story_status("../evil", "s1")


def test_interrupt_story_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.interrupt_story("../evil", "s1")


def test_review_story_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.review_story("../evil", "s1")


def test_mark_story_done_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.mark_story_done("../evil", "s1")


# ---------- MCP tool API surface ----------

def test_completed_dep_ids_is_not_a_public_mcp_tool():
    """_completed_dep_ids is a private helper and must NOT be exposed as an MCP tool."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "_completed_dep_ids" not in tool_names, (
        "_completed_dep_ids is a private helper and must not be a public MCP tool"
    )


def test_list_ready_stories_is_a_public_mcp_tool():
    """list_ready_stories must be exposed as an MCP tool per the documented API."""
    tool_names = {t.name for t in p.mcp._tool_manager.list_tools()}
    assert "list_ready_stories" in tool_names, (
        "list_ready_stories must be decorated with @mcp.tool() to be callable as an MCP tool"
    )


def test_list_ready_stories_rejects_traversal(plan_dir):
    with pytest.raises(ValueError, match="invalid"):
        p.list_ready_stories("../evil")


# ---------- Helper used by the new tests above ----------

def _write_usage_state_direct(path, state):
    """Write state directly to the isolated usage path (bypasses monkeypatching of _atomic_write_json)."""
    path.write_text(json.dumps(state))


@pytest.mark.parametrize("prev_paused,session_pct,week_pct,expected", [
    (False, 50, 10, False),
    (False, 90, 10, True),
    (False, 10, 90, True),
    (False, 89, 10, False),
    (True, 80, 10, True),
    (True, 70, 10, True),
    (True, 69, 10, False),
    (True, 10, 75, True),
])
def test_usage_gate(prev_paused, session_pct, week_pct, expected):
    assert p._usage_gate(prev_paused, session_pct, week_pct) is expected


def test_usage_gate_session_and_week_have_independent_pause_thresholds(monkeypatch):
    monkeypatch.setattr(pusage, "SESSION_PAUSE_THRESHOLD", 80)
    monkeypatch.setattr(pusage, "WEEK_PAUSE_THRESHOLD", 95)

    # Week at 85% would have tripped the old shared 80% threshold, but
    # week's own threshold (95) is not yet reached, and session is low.
    assert p._usage_gate(False, session_pct=10, week_pct=85) is False
    # Session alone crossing its own (lower) threshold still trips it.
    assert p._usage_gate(False, session_pct=80, week_pct=10) is True
    # Week crossing its own (higher) threshold also trips it.
    assert p._usage_gate(False, session_pct=10, week_pct=95) is True


def test_usage_gate_session_and_week_have_independent_resume_thresholds(monkeypatch):
    monkeypatch.setattr(pusage, "SESSION_RESUME_THRESHOLD", 60)
    monkeypatch.setattr(pusage, "WEEK_RESUME_THRESHOLD", 75)

    # Already paused; week is still above its own resume threshold even
    # though it's below the (lower) session resume threshold - stays paused.
    assert p._usage_gate(True, session_pct=10, week_pct=80) is True
    # Both windows have dropped below their own resume thresholds - resumes.
    assert p._usage_gate(True, session_pct=10, week_pct=70) is False


def test_check_usage_carries_paused_hysteresis_from_previous_state(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 80% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is True


def test_check_usage_clears_paused_once_below_resume_threshold(usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 95, "week_pct": 10, "paused": True, "checked_at": "x"}
    ))

    def _fake_run(cmd, **kwargs):
        text = (
            "Current session: 50% used · resets Jun 18 at 11:59am (America/Chicago)\n"
            "Current week (all models): 10% used · resets Jun 23 at 9am (America/Chicago)\n"
        )
        class Result:
            returncode = 0
            stdout = json.dumps({"result": text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()
    assert result["paused"] is False


def test_check_usage_tool_probes_and_persists_state(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 9
    assert result["week_pct"] == 48
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 9
    assert persisted["week_pct"] == 48


def test_check_usage_falls_back_to_last_known_state_when_cli_omits_percentages(
    usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": "old"}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
        "Last 24h · 540 requests · 9 sessions\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["session_pct"] == 91
    assert result["week_pct"] == 60
    assert result["paused"] is True
    assert result["checked_at"] != "old"
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["session_pct"] == 91


def test_check_usage_raises_on_cli_omitting_percentages_with_no_prior_state(
    usage_state_path, monkeypatch,
):
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    with pytest.raises(ValueError):
        p.check_usage()


def test_usage_state_age_seconds_returns_none_when_checked_at_missing():
    assert p._usage_state_age_seconds({}) is None


def test_usage_state_age_seconds_returns_none_when_checked_at_unparseable():
    assert p._usage_state_age_seconds({"checked_at": "old"}) is None


def test_usage_state_age_seconds_returns_elapsed_seconds_for_valid_timestamp():
    checked_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    age = p._usage_state_age_seconds({"checked_at": checked_at})
    assert age is not None
    assert 110 <= age <= 130


def test_check_usage_keeps_paused_when_blackout_is_within_staleness_window(
    usage_state_path, monkeypatch,
):
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": recent}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is True
    assert result.get("stale") is not True


def test_check_usage_clears_pause_when_blackout_outlasts_staleness_window(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps(
        {"session_pct": 91, "week_pct": 60, "paused": True, "checked_at": long_ago}
    ))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True
    persisted = json.loads(usage_state_path.read_text())
    assert persisted["paused"] is False


def test_check_usage_repeated_blackouts_do_not_reset_the_staleness_clock(
    usage_state_path, monkeypatch,
):
    """Each fallback call bumps checked_at to "now" (it's still useful as
    "last time we tried"), so checked_at alone can't be the staleness clock -
    a poller calling check_usage every 60s would perpetually look "fresh" by
    that measure even though the actual session_pct/week_pct have not been
    re-measured in hours. The real measurement time (measured_at) must be
    carried forward unchanged across fallback calls instead."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["stale"] is True


def test_check_usage_fallback_preserves_measured_at_for_the_next_call(
    usage_state_path, monkeypatch,
):
    """measured_at must itself be persisted on every fallback call, not just
    read - otherwise it silently disappears after one call and the next
    call falls back to the (just-bumped) checked_at, recreating the exact
    bug this guards against one call later."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    blackout_text = (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "What's contributing to your limits usage?\n"
    )

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": blackout_text})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    p.check_usage()
    persisted = json.loads(usage_state_path.read_text())

    assert persisted["measured_at"] == long_ago


def test_check_usage_success_path_sets_measured_at(usage_state_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _fake_run)

    result = p.check_usage()

    assert result["measured_at"] == result["checked_at"]


# ---------- Usage gate blind-state visibility ----------
_BLACKOUT_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "What's contributing to your limits usage?\n"
)


def _blackout_run(cmd, **kwargs):
    class Result:
        returncode = 0
        stdout = json.dumps({"type": "result", "result": _BLACKOUT_TEXT})
        stderr = ""
    return Result()


def test_check_usage_marks_gate_blind_when_failing_open(usage_state_path, monkeypatch):
    """When the probe has been dark past the staleness window, failing the gate
    open must be recorded visibly (gate_blind + blind_since), not just printed."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": long_ago, "measured_at": long_ago,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["paused"] is False
    assert result["gate_blind"] is True
    assert result["blind_since"]  # a timestamp was stamped
    assert result["consecutive_parse_failures"] == 1


def test_check_usage_counts_parse_failures_before_going_blind(usage_state_path, monkeypatch):
    """A parse failure still inside the staleness window bumps the counter but
    does not (yet) blind the gate — the last measurement is still trusted."""
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": True,
        "checked_at": recent, "measured_at": recent,
        "consecutive_parse_failures": 2,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["consecutive_parse_failures"] == 3
    assert result.get("gate_blind") is not True
    assert result["paused"] is True  # last measurement still trusted


def test_check_usage_preserves_blind_since_across_consecutive_blind_polls(
    usage_state_path, monkeypatch,
):
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    blind_since = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 60, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": blind_since,
        "consecutive_parse_failures": 5,
    }))
    monkeypatch.setattr(backend.subprocess, "run", _blackout_run)
    monkeypatch.setattr(p, "USAGE_STALE_AFTER_SECONDS", 1800)

    result = p.check_usage()

    assert result["gate_blind"] is True
    assert result["blind_since"] == blind_since  # not reset
    assert result["consecutive_parse_failures"] == 6


def test_check_usage_clears_blind_state_on_successful_probe(usage_state_path, monkeypatch):
    """A real measurement clears the blind flags so the dashboard stops alerting."""
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    usage_state_path.write_text(json.dumps({
        "session_pct": 50, "week_pct": 50, "paused": False,
        "checked_at": long_ago, "measured_at": long_ago,
        "gate_blind": True, "blind_since": long_ago,
        "consecutive_parse_failures": 9,
    }))

    def _ok_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps({"type": "result", "result": SAMPLE_USAGE_TEXT})
            stderr = ""
        return Result()

    monkeypatch.setattr(backend.subprocess, "run", _ok_run)

    result = p.check_usage()

    assert result["gate_blind"] is False
    assert result["consecutive_parse_failures"] == 0
    assert result.get("blind_since") is None


# ---------- Merge adjudication (pure decision) ----------
@pytest.mark.parametrize("autonomy,threshold,verdict,risk,expected", [
    ("gated", "low", "APPROVE", "low", "merge"),
    ("gated", "low", "APPROVE", "medium", "park"),
    ("gated", "medium", "APPROVE", "medium", "merge"),
    ("gated", "low", "REQUEST_CHANGES", "low", "park"),
    ("full", "low", "APPROVE", "medium", "merge"),
    ("full", "low", "APPROVE", "high", "park"),
    ("dry-run", "high", "APPROVE", "low", "park"),
])
def test_merge_decision(monkeypatch, autonomy, threshold, verdict, risk, expected):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", autonomy)
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", threshold)
    story = {"review_verdict": verdict, "risk": risk}
    assert p._merge_decision(story)["action"] == expected


# ---------- advance_pipeline concurrency lock ----------
# Overlapping advance_pipeline ticks for the same plan (e.g. launchd firing a
# burst of missed StartIntervals after the machine wakes from sleep) must not
# both see the same ready story and dispatch duplicate, colliding agents into
# the same worktree - that's what actually caused repeated zero-output agent
# deaths in production, not per-story flakiness.
def test_advance_pipeline_skips_when_another_tick_holds_the_lock(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out tick must not dispatch anything")
    monkeypatch.setattr(p, "dispatch_story", _boom)

    lock_path = plan_dir / "lk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lk")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "lk")
    assert manifest["stories"]["T1"]["status"] == "todo"


def test_advance_pipeline_proceeds_when_lock_is_free(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lk2", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("lk2")

    assert result.get("skipped") is None
    assert dispatched == ["T1"]


def test_advance_pipeline_releases_lock_after_each_call(plan_dir, monkeypatch):
    # A held-then-released lock (the normal case: one tick finishes before
    # the next starts) must not leak into a permanent skip.
    _write_manifest(plan_dir, "lk3", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("lk3")
    result = p.advance_pipeline("lk3")

    assert result.get("skipped") is None


def test_advance_pipeline_lock_is_independent_per_plan(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "lkA", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    _write_manifest(plan_dir, "lkB", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append((plan, key)))

    lock_path = plan_dir / "lkA.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.advance_pipeline("lkB")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.get("skipped") is None
    assert ("lkB", "T1") in dispatched


# ---------- dispatch_story / interrupt_story _plan_lock serialization ----------
# Two Claude sessions (each with their own MCP server PID) calling
# dispatch_story on the same story in the same window both want to write
# the same manifest and create the same worktree. Without _plan_lock on
# these tools, the second caller treats the first's half-built worktree
# as resumable and spawns a second agent into the same directory. These
# tests confirm the lock is held for both tools and that a held lock makes
# the call return cleanly instead of crashing or racing.

def test_dispatch_story_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "dlk", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out dispatch_story must not touch the worktree or manifest")
    monkeypatch.setattr(p.subprocess, "run", _boom)
    monkeypatch.setattr(pt, "plane_request", _boom)

    lock_path = plan_dir / "dlk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.dispatch_story("dlk", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert "another dispatch/interrupt" in result.get("reason", "")
    # Manifest untouched (still "todo", no pid written).
    manifest = _read_manifest(plan_dir, "dlk")
    assert manifest["stories"]["S1"]["status"] == "todo"
    assert "pid" not in manifest["stories"]["S1"]


def test_interrupt_story_skips_when_lock_held(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ilk", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    def _boom(*a, **k):
        raise AssertionError("a locked-out interrupt_story must not signal or checkpoint")
    monkeypatch.setattr(p.os, "kill", _boom)
    monkeypatch.setattr(p.subprocess, "run", _boom)

    lock_path = plan_dir / "ilk.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.interrupt_story("ilk", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    assert "another dispatch/interrupt" in result.get("reason", "")
    # Manifest untouched: still in_progress with its original pid.
    manifest = _read_manifest(plan_dir, "ilk")
    assert manifest["stories"]["S1"]["status"] == "in_progress"
    assert manifest["stories"]["S1"]["pid"] == 4242


# ---------- advance_pipeline nested _plan_lock regression ----------
# advance_pipeline holds _plan_lock for the whole tick and calls dispatch_story
# and interrupt_story, which each re-acquire the same lock. flock is per
# open-file-description, so a second os.open of the lock file fails to re-flock
# within the same process — the nested call used to return skipped:locked and
# advance_pipeline would falsely count it as dispatched/interrupted while doing
# nothing. _plan_lock must be reentrant within a tick so the nested call
# actually runs. These exercise the REAL dispatch_story/interrupt_story (only
# external boundaries mocked), not stubs.

def test_advance_pipeline_actually_dispatches_ready_story_not_just_reports_it(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "nest", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    # check_story_status would try to run tests against an empty mock worktree;
    # the dispatch itself is what we're asserting, so keep the agent "running".
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    result = p.advance_pipeline("nest")

    assert result["ok"] is True
    assert "S1" in result["dispatched"]
    story = _read_manifest(plan_dir, "nest")["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 1234


def test_advance_pipeline_actually_interrupts_in_progress_when_dispatch_gated(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    _write_manifest(plan_dir, "nestint", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wt"), "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    # Dispatch backend gated -> advance_pipeline interrupts in_progress agents.
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (False, "gate down"))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("nestint")

    assert result["ok"] is True
    assert result["dispatch_paused"] is True
    assert "R1" in result["interrupted"]
    story = _read_manifest(plan_dir, "nestint")["stories"]["R1"]
    assert story["status"] == "interrupted"


def test_advance_pipeline_does_not_interrupt_in_progress_on_memory_pressure_gate(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """A local-memory-pressure gate ('insufficient free memory') is
    self-inflicted by an in-progress dispatch actively loading its model -
    killing it doesn't free a shared/exhaustible resource, it just destroys
    progress and immediately re-triggers the same gate on redispatch.
    Observed live 2026-07-13: a repeating load/interrupt/redispatch cycle
    (a new PID every ~10-20s, never converging) on both glm-4.7-flash and
    qwen3-coder:30b. Only THIS specific reason should be exempted from the
    interrupt sweep; test_advance_pipeline_actually_interrupts_in_progress_
    when_dispatch_gated covers that every other gate reason still
    interrupts as before."""
    _write_manifest(plan_dir, "memgate", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111,
               "worktree": str(tmp_path / "wt"), "dependencies": []},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (False, "insufficient free memory (1024mb < 2048mb floor)"),
    )
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "running"})

    class _GitResult:
        returncode = 0
        stdout = "sha123\n"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: _GitResult())

    result = p.advance_pipeline("memgate")

    assert result["ok"] is True
    assert result["dispatch_paused"] is True
    assert result["interrupted"] == []
    story = _read_manifest(plan_dir, "memgate")["stories"]["R1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 111


def test_dispatch_story_proceeds_when_lock_free(plan_dir, worktree_root, agents_dir, monkeypatch):
    """Sanity check: with no lock held, dispatch_story runs normally. Catches
    a regression where the lock is held unconditionally (no caller would ever
    proceed) or released too early (a second call races in mid-dispatch)."""
    _write_manifest(plan_dir, "dlk2", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1357))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dlk2", "S1")

    assert result["ok"] is True
    assert result["pid"] == 1357
    assert result.get("skipped") is None


# ---------- Heavy-build lock (Fix C) ----------
# Three concurrent cold builds can push a 24GB M4 to its knees (observed in
# the post-PR #30 e2e rerun). _heavy_lock serializes cargo/npm/mvn/gradle/etc.
# invocations across every dispatch site. _is_heavy() is the static predicate
# that decides what counts as a heavy command.

def test_heavy_executables_set_is_static_and_reasonable():
    """Sanity: the static list of heavy executables covers the obvious
    build runners and nothing silly. If someone adds `python` here it'll
    show up in this test failure."""
    expected = {
        "cargo", "npm", "yarn", "pnpm", "npx",
        "mvn", "gradle", "./gradlew",
        "sbt", "bazel", "buck",
        "go", "rustc", "swift", "swiftc",
    }
    assert p.HEAVY_EXECUTABLES == frozenset(expected), (
        f"HEAVY_EXECUTABLES changed unexpectedly: {p.HEAVY_EXECUTABLES - frozenset(expected)} "
        f"added, {frozenset(expected) - p.HEAVY_EXECUTABLES} removed"
    )


@pytest.mark.parametrize("cmd,expected", [
    (["cargo", "build", "-p", "foo"], True),
    (["cargo"], True),
    (["cargo-fmt"], False),  # different executable, different process
    (["npm", "test"], True),
    (["npx", "vitest"], True),
    (["pnpm", "install"], True),
    (["./gradlew", "test"], True),
    (["make", "test"], True),
    (["make", "build"], True),
    (["make", "ci"], True),
    (["make", "all"], True),
    (["make", "clean"], False),  # trivial target
    (["make", "install"], False),  # not in heavy list
    (["make"], False),  # no target
    (["pytest"], False),
    (["ls", "-la"], False),
    (["git", "log"], False),
    (["rustc", "main.rs"], True),
    (["swift", "build"], True),
    (["swiftc", "main.swift"], True),
    ([], False),
])
def test_is_heavy_predicate(cmd, expected):
    """Static executable list catches the build runners without parsing
    command bodies. False negatives (missing a heavy command) are fine —
    a wedged build just doesn't get locked. False positives (locking a
    trivial command) would add latency, so the list is conservative."""
    assert p._is_heavy(cmd) is expected, f"_is_heavy({cmd}) != {expected}"


def test_heavy_lock_serializes_two_concurrent_holders(tmp_path):
    """Two threads entering _heavy_lock() cannot hold it simultaneously.
    Blocking acquire (LOCK_EX) means the second thread queues until the
    first releases."""
    import threading
    # Use tmp_path as PLAN_DIR so the lock file lives in a clean spot
    # for this test only (no risk of colliding with other tests).
    monkeypath_lock = tmp_path / "heavy.lock"
    fd1 = os.open(str(monkeypath_lock), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold the real lock

    start = time.monotonic()
    held_during_wait = []

    def _contender():
        # The real lock is held by fd1; this open() will block on flock.
        fd2 = os.open(str(monkeypath_lock), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX)  # BLOCKING — queues behind fd1
            held_during_wait.append(time.monotonic())
        finally:
            fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)

    t = threading.Thread(target=_contender)
    t.start()
    time.sleep(0.3)  # let the contender queue
    fcntl.flock(fd1, fcntl.LOCK_UN)  # release
    os.close(fd1)
    t.join(timeout=2.0)

    # Contender should have entered the critical section only after we
    # released fd1, i.e. at least ~0.3s after start.
    assert held_during_wait, "contender never acquired the lock"
    assert held_during_wait[0] - start >= 0.25, (
        f"contender should have waited for the holder to release; "
        f"got in at {held_during_wait[0] - start:.3f}s"
    )


def test_check_story_status_acquires_heavy_lock_for_cargo(
    plan_dir, worktree_root, monkeypatch,
):
    """The orchestrator's cargo test grading must take the heavy lock so
    it serializes against in-flight agent cargo invocations. We assert
    by counting flock acquisitions on PLAN_DIR/heavy.lock during the call.
    """
    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    # Write enough manifest state for check_story_status to not bail early.
    manifest_path = plan_dir / "cargo_lock.manifest.json"
    manifest_path.write_text(json.dumps({
        "stories": {
            "S1": {
                "summary": "x", "agent_instructions": "x",
                "status": "in_progress", "dependencies": [],
                "pid": 99999,  # a pid we'll kill below
                "worktree": str(wt),
                "log": str(wt / "agent.log"),
            },
        },
    }))

    # Make sure the pid is dead so check_story_status proceeds past the
    # liveness check.
    try:
        os.kill(99999, 0)
        # If we got here, pid 99999 is alive — try another (very unlikely).
        # We don't need to be precise; the test below catches the lock.
        skip_pid = True
    except ProcessLookupError:
        skip_pid = False

    if skip_pid:
        return  # can't reliably exercise the path

    (wt / "agent.log").write_text("[step 0] bash: pwd\n")  # non-empty log

    lock_held_during_cargo = []
    real_flock = fcntl.flock

    def _counting_flock(fd, op):
        real_flock(fd, op)
        # PLAN_DIR is set in conftest; the heavy lock file lives there.
        heavy_lock = plan_dir / "heavy.lock"
        if (op & fcntl.LOCK_EX) and not (op & fcntl.LOCK_NB):
            try:
                # Probe: can we acquire LOCK_EX | LOCK_NB right now? If no,
                # someone else holds the lock — that's exactly what we want.
                probe_fd = os.open(str(heavy_lock), os.O_CREAT | os.O_RDWR)
                try:
                    real_flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # We got it — heavy lock is free.
                except BlockingIOError:
                    lock_held_during_cargo.append(True)
                finally:
                    try:
                        real_flock(probe_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(probe_fd)
            except OSError:
                pass

    monkeypatch.setattr(pcon.fcntl, "flock", _counting_flock)

    # Stub cargo so the test doesn't actually compile.
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout="", stderr=""))

    p.check_story_status("cargo_lock", "S1")

    assert lock_held_during_cargo, (
        "check_story_status did not acquire heavy.lock for a cargo worktree"
    )


def test_check_story_status_strips_pipeline_env_from_test_subprocess(
    plan_dir, worktree_root, monkeypatch,
):
    """Mode 6: the test-grading subprocess must NOT inherit the MCP server's
    PIPELINE_* operational env. Those vars (PIPELINE_PAUSE_THRESHOLD,
    PIPELINE_BACKEND_DISPATCH, PIPELINE_LOCAL_MODEL_DEFAULT, ...) override the
    defaults the suite asserts against and false-fail Python stories at the
    gate (10 env-sensitive tests fail under the server env, 303 pass clean).
    The gate grades the agent's work in a clean dev env, not the server's
    operational one.
    """
    try:
        os.kill(99999, 0)
        return  # pid unexpectedly alive; can't exercise the path reliably
    except ProcessLookupError:
        pass

    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest_path = plan_dir / "envstrip.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {"S1": {
        "summary": "x", "agent_instructions": "x", "status": "in_progress",
        "dependencies": [], "pid": 99999, "worktree": str(wt),
        "log": str(wt / "agent.log"),
    }}}))
    (wt / "agent.log").write_text("[step 0] bash: pwd\n")

    # Operational env the MCP server carries -- must NOT reach the test run.
    for k, v in [
        ("PIPELINE_PAUSE_THRESHOLD", "101"),
        ("PIPELINE_RESUME_THRESHOLD", "0"),
        ("PIPELINE_WEEK_PAUSE_THRESHOLD", "100"),
        ("PIPELINE_BACKEND_DISPATCH", "auto"),
        ("PIPELINE_LOCAL_MODEL_DEFAULT", "minimax-m3:cloud"),
        # LOCAL_AGENT_* harness config the scheduler plist may set for a run
        # (e.g. READ_HEAVY_DISTINCT_WINDOWS raised so a model can explore
        # longer). test_local_agent.py asserts the DEFAULTS, so an override
        # that survives into the graded run false-fails the suite for every
        # story in a repo that vendors the pipeline's own tests.
        ("LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS", "6"),
        ("LOCAL_AGENT_READ_HEAVY_WINDOW", "12"),
        ("LOCAL_AGENT_CHAT_MAX_ATTEMPTS", "9"),
        # REPO_ROOT is a per-plan sentinel, not a developer default.
        ("REPO_ROOT", "/nonexistent-repo-root-set-per-plan-only"),
    ]:
        monkeypatch.setenv(k, v)

    marker = "__mode6_test_marker__"
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(wt), [marker, "pytest"]))
    monkeypatch.setattr(p, "_is_heavy", lambda cmd: False)

    captured = []
    def _capture(cmd, **kw):
        if cmd and cmd[0] == marker:
            captured.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="3 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _capture)

    p.check_story_status("envstrip", "S1")

    assert captured, "test-grading subprocess was never run"
    test_env = captured[0]
    assert test_env is not None, (
        "check_story_status passed no env= to the test subprocess, so it "
        "inherited the MCP server's PIPELINE_* env unchanged")
    leaked = [k for k in test_env if k.startswith("PIPELINE_")]
    assert not leaked, f"test subprocess inherited PIPELINE_* env: {leaked}"
    # LOCAL_AGENT_* harness config and the per-plan REPO_ROOT sentinel must
    # also be stripped — they override defaults the suite asserts against.
    leaked_local = [k for k in test_env if k.startswith("LOCAL_AGENT_")]
    assert not leaked_local, (
        f"test subprocess inherited LOCAL_AGENT_* env: {leaked_local}")
    assert "REPO_ROOT" not in test_env, (
        "test subprocess inherited the per-plan REPO_ROOT sentinel")
    # Sanity: the rest of the environment (PATH etc.) is preserved.
    assert "PATH" in test_env


def test_check_story_status_records_sha_on_last_test_and_lint_check(
    plan_dir, worktree_root, monkeypatch,
):
    """The persisted last_test_check / last_lint_check must carry the worktree's
    current HEAD sha so later dispatch/review/rebrief logic can detect when the
    cache is stale (recorded at a past commit) and refuse to reuse it."""
    try:
        os.kill(99999, 0)
        return  # pid unexpectedly alive; can't exercise the path reliably
    except ProcessLookupError:
        pass

    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    manifest_path = plan_dir / "sha.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {"S1": {
        "summary": "x", "agent_instructions": "x", "status": "in_progress",
        "dependencies": [], "pid": 99999, "worktree": str(wt),
        "log": str(wt / "agent.log"),
    }}}))
    (wt / "agent.log").write_text("[step 0] bash: pwd\n")

    marker = "__sha_test_marker__"
    lint_marker = "__sha_lint_marker__"
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(wt), [marker, "pytest"]))
    monkeypatch.setattr(p, "detect_lint_command",
                        lambda wt: (str(wt), [lint_marker, "lint"]))
    monkeypatch.setattr(p, "_is_heavy", lambda cmd: False)

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == marker:
            return subprocess.CompletedProcess(cmd, 0, stdout="3 passed", stderr="")
        if cmd and cmd[0] == lint_marker:
            return subprocess.CompletedProcess(cmd, 0, stdout="no lint issues", stderr="")
        if cmd and cmd[0] == "git" and cmd[1] == "rev-parse":
            return subprocess.CompletedProcess(cmd, 0, stdout="aaa111\n", stderr="")
        # git diff / show / grep used by _find_dead_new_functions: no output.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.check_story_status("sha", "S1")

    story = json.loads(manifest_path.read_text())["stories"]["S1"]
    assert story["last_test_check"]["sha"] == "aaa111"
    assert story["last_lint_check"]["sha"] == "aaa111"



# An empty agent.log within the startup grace window is "agent is alive and
# bootstrapping" (its first print() hasn't flushed — Ollama -np 1 can take
# 30-90s to respond). Outside the window it's the genuine "agent never
# produced any output" failed-launch signature.

def test_check_story_status_treats_empty_log_as_running_within_grace(
    plan_dir, tmp_path, monkeypatch,
):
    """A 0-byte log with a fresh mtime means the agent is still alive and
    bootstrapping (e.g. queued on Ollama's -np 1 worker). check_story_status
    must return "running" so the orchestrator doesn't burn dispatch_attempts
    on a process that's just slow to print."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 90)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")  # 0-byte, mtime ~= now
    _write_manifest(plan_dir, "g1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": os.getpid(),  # self → os.kill succeeds, "alive"
               "worktree": str(worktree), "dispatch_attempts": 0},
    })
    # tests must not run — we're declaring it still-running.
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    result = p.check_story_status("g1", "S1")

    assert result["status"] == "running"
    # Crucially, dispatch_attempts was NOT incremented: a live-but-slow
    # agent must not be retried/redispatched yet.
    story = _read_manifest(plan_dir, "g1")["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["dispatch_attempts"] == 0


def test_check_story_status_treats_empty_log_as_failed_launch_after_grace(
    plan_dir, tmp_path, monkeypatch,
):
    """Outside the grace window, an empty log is the original failed-launch
    signature (the agent never produced any output and the process is now
    dead). Status must advance to "interrupted" (or, after budget exhaustion,
    "failed") — NOT stay "running" forever."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 90)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("")
    # Backdate the log's mtime past the grace window.
    old_time = time.time() - 200
    os.utime(log_path, (old_time, old_time))
    _write_manifest(plan_dir, "g2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree), "dispatch_attempts": 0},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("tests must not run")))

    result = p.check_story_status("g2", "S1")

    assert result["status"] == "interrupted"
    story = _read_manifest(plan_dir, "g2")["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["dispatch_attempts"] == 1


def test_check_story_status_kills_hung_process_past_watchdog_timeout(
    plan_dir, tmp_path, monkeypatch,
):
    """A dispatch subprocess that's still alive well past the watchdog ceiling
    is a hang, not legitimate progress — observed directly during MLX
    provider validation: a blocking, non-streaming chat call can stall
    indefinitely on a single stuck request (0% CPU, no error), and the outer
    harness's own timeout never killed the orphaned subprocess (found running
    minutes later, had to be killed manually). Past the ceiling,
    check_story_status must terminate the process and checkpoint rather than
    reporting "running" forever."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 60)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    old_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    _write_manifest(plan_dir, "wd1", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": old_dispatched_at},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            if cmd[0] == "ps":
                stdout = "S\n"
            elif cmd[:2] == ["git", "rev-parse"]:
                stdout = "sha-wd\n"
            else:
                stdout = ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd1", "S1")

    assert (4242, pcheckpoint.signal.SIGTERM) in killed
    assert result["status"] == "interrupted"
    assert result.get("watchdog_killed") is True

    story = _read_manifest(plan_dir, "wd1")["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert "dispatch_error" in story
    journal = json.loads((plan_dir / "wd1.S1.journal.json").read_text())
    assert journal[-1]["step"] == "dispatch_watchdog_timeout"


def test_check_story_status_running_within_watchdog_window_is_not_killed(
    plan_dir, tmp_path, monkeypatch,
):
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 3600)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    recent_dispatched_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    _write_manifest(plan_dir, "wd2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_at": recent_dispatched_at},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "S\n" if cmd[0] == "ps" else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd2", "S1")

    assert result == {"status": "running", "pid": 4242}
    assert pcheckpoint.signal.SIGTERM not in [sig for _, sig in killed]
    story = _read_manifest(plan_dir, "wd2")["stories"]["S1"]
    assert story["status"] == "in_progress"


def test_check_story_status_running_without_dispatched_at_skips_watchdog(
    plan_dir, tmp_path, monkeypatch,
):
    """A story dispatched before this field existed has no dispatched_at —
    check_story_status must not crash and must not spuriously kill it; the
    watchdog simply can't apply without a known start time."""
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 1)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_manifest(plan_dir, "wd3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "S\n" if cmd[0] == "ps" else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("wd3", "S1")

    assert result == {"status": "running", "pid": 4242}


# ---------- advance_pipeline orchestration ----------

def test_advance_pipeline_does_not_report_skipped_locked_as_dispatched(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "skipplan", {"PIPE-9": {"status":"todo"}})
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: {"ok": True, "skipped": "locked"})
    result = p.advance_pipeline("skipplan")
    assert "PIPE-9" not in result.get("dispatched", [])

# advance_pipeline is a coordinator; the per-story operations (dispatch_story,
# check_story_status, review_story, gh merge) are exercised by their own tests
# above, so here we substitute test doubles to verify routing and gating.
def test_advance_pipeline_dry_run_has_no_side_effects(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "dry-run")
    _write_manifest(plan_dir, "dr", {
        "T1": {"summary": "todo one", "status": "todo", "dependencies": []},
        "P1": {"summary": "ready pr", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })

    def _boom(*a, **k):
        raise AssertionError("dry-run must not take actions")

    monkeypatch.setattr(p, "dispatch_story", _boom)
    monkeypatch.setattr(p, "_merge_pr", _boom)

    result = p.advance_pipeline("dr")
    assert result["dry_run"] is True
    assert "T1" in result["would_dispatch"]
    assert "P1" in result["would_merge_decisions"]


def test_advance_pipeline_gated_dispatches_merges_and_parks(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "go", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "P1": {"summary": "low approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
        "P2": {"summary": "high approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/y"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.advance_pipeline("go")
    assert dispatched == ["T1"]
    assert merged == ["P1"]
    assert "P1" in result["merged"]
    assert "P2" in result["parked"]
    assert "P2" in result["notify"]

    manifest = _read_manifest(plan_dir, "go")
    assert manifest["stories"]["P1"]["status"] == "done"
    assert manifest["stories"]["P2"]["status"] == "parked"


def test_advance_pipeline_merge_transitions_plane_issue_to_done(plan_dir, monkeypatch):
    # A story merged via advance_pipeline's automatic path must move the
    # Plane issue to Done too - otherwise it stays "In Progress" forever,
    # since dispatch_story is the only other place that touches Plane state.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    story_key = "11111111-1111-1111-1111-111111111111"
    _write_manifest(plan_dir, "planedone", {
        story_key: {"summary": "approved", "status": "pr_open",
                     "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    patches = []
    monkeypatch.setattr(pt, "plane_request",
        lambda method, path, **kw: patches.append((method, path, kw)),
    )

    p.advance_pipeline("planedone")

    assert ("PATCH", f"/projects/{pt.PLANE_PROJECT}/work-items/{story_key}/",
            {"json": {"state": "state-completed"}}) in patches


def test_advance_pipeline_merge_tolerates_plane_failure(plan_dir, monkeypatch):
    # Mirrors dispatch_story's resilience: not every plan is Plane-backed, so
    # a Plane error must not block the local merge from completing.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "planefail", {
        "P1": {"summary": "approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    result = p.advance_pipeline("planefail")

    assert result["merged"] == ["P1"]
    assert _read_manifest(plan_dir, "planefail")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_parked_approved_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am", {
        "P1": {"summary": "approved but medium risk", "status": "parked",
               "review_verdict": "APPROVE", "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    plane_calls = []
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: plane_calls.append(key))

    result = p.approve_merge("am", "P1")

    assert result["ok"] is True
    assert result["status"] == "done"
    assert merged == ["P1"]
    assert plane_calls == ["P1"]
    assert _read_manifest(plan_dir, "am")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_pr_open_approved_story(plan_dir, monkeypatch):
    # A human may approve before the gate even runs (status still pr_open),
    # not only after it's been parked.
    _write_manifest(plan_dir, "am2", {
        "P1": {"summary": "approved, not yet adjudicated", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("am2", "P1")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "am2")["stories"]["P1"]["status"] == "done"


def test_approve_merge_rejects_story_without_approve_verdict(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am3", {
        "P1": {"summary": "changes requested", "status": "parked",
               "review_verdict": "REQUEST_CHANGES", "risk": "medium", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am3", "P1")

    assert result["ok"] is False
    assert merge_calls == []
    assert _read_manifest(plan_dir, "am3")["stories"]["P1"]["status"] == "parked"


@pytest.mark.parametrize("status", ["todo", "in_progress", "done", "failed", "interrupted"])
def test_approve_merge_rejects_story_in_non_mergeable_status(plan_dir, monkeypatch, status):
    _write_manifest(plan_dir, "am4", {
        "P1": {"summary": "not ready", "status": status,
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am4", "P1")

    assert result["ok"] is False
    assert merge_calls == []


def test_approve_merge_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "am5", {})
    result = p.approve_merge("am5", "nope")
    assert result["ok"] is False


def test_approve_merge_uses_plan_repo_root(plan_dir, monkeypatch, tmp_path):
    real_repo = tmp_path / "real-repo"
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    _write_manifest(plan_dir, "am6", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": str(tmp_path / "wt")},
    })
    manifest_path = plan_dir / "am6.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    seen_repo_roots = []

    def _fake_merge_pr(wt, key):
        seen_repo_roots.append(p.REPO_ROOT)
        return "merged"

    monkeypatch.setattr(p, "_merge_pr", _fake_merge_pr)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    p.approve_merge("am6", "P1")

    assert seen_repo_roots == [real_repo]
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_advance_pipeline_paused_interrupts_running_and_skips_new_work(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "pause", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed",
                "worktree": "/y", "risk": "low"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("pause")

    assert result["paused"] is True
    assert dispatched == []
    assert reviewed == []
    assert interrupted == ["R1"]
    assert "R1" in result["interrupted"]


def test_role_resource_ok_auto_does_not_crash_and_is_ok_when_local_available(
    usage_state_path, monkeypatch,
):
    """PIPELINE_BACKEND_<ROLE>=auto must not reach get_backend with the literal
    'auto' (which raises ValueError). Under auto the role can take work whenever
    the local backend is healthy, even with Claude's usage gate tripped."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is True
    assert reason == ""


def test_role_resource_ok_auto_gated_only_when_both_backends_unavailable(
    usage_state_path, monkeypatch,
):
    """Under auto, the role is gated only when BOTH local and Claude are down;
    it then surfaces Claude's gate reason."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": False, "reason": "Ollama unreachable"})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_role_resource_ok_review_uses_plan_role_config_backend_not_claude(
    usage_state_path, monkeypatch,
):
    """A plan that pins review to a local provider (e.g. ollama/glm) must be
    gated by THAT provider's resource_status(), not Claude's usage poller. With
    Claude's gate tripped (session paused) but Ollama healthy, review must be
    ok — this is the mode30 E2E incident: review was permanently deferred as
    review_paused while Claude usage sat at 100% even though review never
    touches Claude."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is True
    assert reason == ""


def test_role_resource_ok_review_plan_role_config_gates_when_local_down(
    usage_state_path, monkeypatch,
):
    """Negative side: when the plan-pinned review backend (ollama) is down,
    review must be gated with that backend's reason — even if Claude is
    healthy. The gate follows the plan role_config, not the env default."""
    usage_state_path.write_text(json.dumps({"session_pct": 5, "week_pct": 5, "paused": False}))
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": False, "reason": "Ollama unreachable"})

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is False
    assert reason == "Ollama unreachable"


def test_role_resource_ok_review_garbage_plan_provider_fails_open_to_env(
    usage_state_path, monkeypatch,
):
    """A garbage provider in plan role_config must not crash advance_pipeline:
    the gate fails open to the env-based path. With env review=claude and
    Claude's gate tripped, that fallback yields ok=False (Claude's reason) —
    the point is it doesn't raise and it doesn't silently ok=True."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "claude")

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "not-a-real-backend"}}
    )

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_role_resource_ok_dispatch_ignores_plan_role_config(
    usage_state_path, monkeypatch,
):
    """dispatch's real backend is per-story via _route_dispatch_backend
    (env-local-first), NOT role_registry — so plan_role_config must NOT
    redirect the dispatch gate to a registry provider. With env dispatch=local
    and a plan role_config that pins review (not dispatch), the dispatch gate
    still checks the local backend and ignores the plan config entirely."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok(
        "dispatch", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is True
    assert reason == ""


def test_list_ready_stories_resolves_summary_dependencies(plan_dir):
    """Dependencies expressed as a prerequisite's summary string (the documented
    save_plan schema) must resolve against done stories even when the manifest is
    keyed by UUID rather than by summary."""
    _write_manifest(plan_dir, "sdep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Builds on foundation", "status": "todo",
                   "dependencies": ["Foundation"]},
        "uuid-c": {"summary": "Blocked", "status": "todo",
                   "dependencies": ["Builds on foundation"]},
    })

    ready = p.list_ready_stories("sdep")

    assert [r["summary"] for r in ready] == ["Builds on foundation"]


def test_advance_pipeline_dispatches_story_with_summary_dependency(plan_dir, monkeypatch):
    """The dispatch tick must treat a satisfied summary-string dependency as met
    and dispatch the dependent story, not silently skip it forever."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    _write_manifest(plan_dir, "adep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Next", "status": "todo", "dependencies": ["Foundation"]},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    p.advance_pipeline("adep")

    assert dispatched == ["uuid-b"]


def test_advance_pipeline_local_dispatch_runs_while_claude_review_gated(
    plan_dir, usage_state_path, monkeypatch,
):
    """Step 5: dispatch on local + review on Claude. Claude usage is maxed,
    but local dispatch must still proceed (and not interrupt running local
    agents); only the Claude-backed review is deferred."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)  # -> claude
    # Local backend reports healthy without hitting a real Ollama.
    monkeypatch.setattr(backend.OllamaDriver, "resource_status", lambda self: {"ok": True, "reason": ""})

    _write_manifest(plan_dir, "split", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed", "worktree": "/y", "risk": "low"},
    })
    dispatched, reviewed, interrupted = [], [], []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "review_story", lambda plan, key: reviewed.append(key) or {"status": "pr_open"})
    monkeypatch.setattr(p, "interrupt_story", lambda plan, key: interrupted.append(key) or {"ok": True})
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    result = p.advance_pipeline("split")

    assert result["dispatch_paused"] is False     # local dispatch not gated by Claude
    assert result["review_paused"] is True         # Claude review deferred
    assert dispatched == ["T1"]                     # dispatch proceeded
    assert interrupted == []                         # running local agent left alone
    assert reviewed == []                            # review deferred


def test_advance_pipeline_paused_still_processes_merges(plan_dir, usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "pausemerge", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")

    result = p.advance_pipeline("pausemerge")
    assert merged == ["P1"]
    assert "P1" in result["merged"]


def test_advance_pipeline_merge_failure_retries_within_budget(plan_dir, monkeypatch):
    # A transient _merge_pr failure (e.g. gh hiccup) must not crash the tick or
    # burn the story: it stays pr_open with a bumped attempt counter so the next
    # tick retries, and the user is notified.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergeretry", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh hiccup")))

    result = p.advance_pipeline("mergeretry")

    story = _read_manifest(plan_dir, "mergeretry")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_failure_exhausts_budget(plan_dir, monkeypatch):
    # Once the attempt budget is spent, a persistently failing merge becomes a
    # hard failure that needs human intervention rather than retrying forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergegiveup", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 2},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("mergegiveup")

    story = _read_manifest(plan_dir, "mergegiveup")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 3
    assert "still broken" in story.get("merge_error", "")
    assert result["merged"] == []
    assert "P1" in result["failed"]
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_success_clears_attempt_counter(plan_dir, monkeypatch):
    # A merge that finally succeeds after earlier failures must clear the
    # attempt counter so the story records a clean done.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "mergerecover", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 1},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("mergerecover")

    story = _read_manifest(plan_dir, "mergerecover")["stories"]["P1"]
    assert story["status"] == "done"
    assert "merge_attempts" not in story
    assert result["merged"] == ["P1"]


# ---- Mode 9: rebase-before-merge + CI gate -----------------------------


def test_rebase_onto_master_skips_when_worktree_missing(monkeypatch, tmp_path):
    # A missing/anomalous worktree cannot be rebased; the helper falls back to
    # ok so the gate degrades to CI + the original conflict-at-merge check
    # rather than blocking forever on a path that doesn't exist.
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(tmp_path / "does-not-exist"), "agent/x")
    assert rb["ok"] is True
    assert rb["conflict"] is False


def test_rebase_onto_master_reports_conflict(monkeypatch, tmp_path):
    # When `git rebase` fails on a conflict, the helper aborts the rebase and
    # reports conflict=True so the caller can park rather than retry blindly.
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()  # make Path(wt).is_dir() true; git itself is faked

    def _fake_run(argv, cwd, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "error: could not apply ... fix conflicts"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True


def test_rebase_onto_master_uses_default_branch_on_main_repo(tmp_path, monkeypatch):
    # Gap from the live-`gh` probe (PROOF.md note #1): when the repo's default
    # branch is `main` (e.g. a fresh `gh repo create`), the rebase path must
    # rebase onto `origin/main`, not the hardcoded `origin/master`. A `main`-
    # default repo with no `master` ref would otherwise fail the rebase and
    # block every merge-gate attempt. Run against a real tmp git repo so the
    # `git rebase` invocation actually executes against the configured ref.
    # `wt` is a real `git worktree add` off `repo` (not a separate `git init`)
    # because that's the production invariant: the fetch into REPO_ROOT updates
    # `origin/main` for the worktree too, since they share `.git/`.
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    for d in (repo,):
        r = subprocess.run(["git", "init", "-q", "-b", "main", str(d)],
                           check=False, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        subprocess.run(["git", "config", "user.email", "t@e"],
                       cwd=d, capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=d, capture_output=True, text=True, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, capture_output=True, text=True, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                   cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", "main"],
                   cwd=repo, capture_output=True, text=True, check=True)
    # Real worktree off `repo` so it shares `.git/`. A new commit on
    # `agent/x` from the worktree gives the rebase something to fast-forward.
    r = subprocess.run(["git", "worktree", "add", "-b", "agent/x", str(wt)],
                       check=False, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (wt / "new.txt").write_text("agent edit\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "agent edit"], cwd=wt,
                   capture_output=True, text=True, check=True)
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True, f"rebase failed: {rb}"
    assert rb["conflict"] is False


def _setup_conflict_repo(tmp_path, files_base, agent_edits, master_edits):
    """Build repo+bare origin+worktree with a base commit (`files_base`: full
    file contents), then a divergent commit on the worktree's `agent/x`
    branch (`agent_edits`: full new file contents) and a divergent commit
    pushed to `origin/master` (`master_edits`: full new file contents) - so
    rebasing `agent/x` onto `origin/master` conflicts on every file present
    in both edit dicts. Returns (repo, wt) Paths."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   capture_output=True, text=True, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", str(origin)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo,
                   capture_output=True, text=True, check=True)
    for name, content in files_base.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=repo,
                   capture_output=True, text=True, check=True)

    r = subprocess.run(["git", "worktree", "add", "-b", "agent/x", str(wt)],
                       check=False, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name, content in agent_edits.items():
        (wt / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "agent edit"], cwd=wt,
                   capture_output=True, text=True, check=True)

    for name, content in master_edits.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "master edit"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=repo,
                   capture_output=True, text=True, check=True)
    return repo, wt


def test_rebase_auto_resolves_additive_import_conflict(tmp_path, monkeypatch):
    # The primary positive case: two branches each add a distinct import line
    # at the same anchor point in a shared file. Must auto-resolve (union of
    # both added lines) and continue the rebase rather than aborting.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os\nimport json\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True, f"expected auto-resolved rebase, got {rb}"
    assert rb["conflict"] is False
    assert rb.get("auto_resolved") is True
    result = (wt / "shared.py").read_text()
    assert "import sys" in result
    assert "import json" in result


def test_rebase_aborts_when_a_side_modifies_existing_line(tmp_path, monkeypatch):
    # A conflict where one side modifies a PRE-EXISTING line (not a pure
    # addition) must never auto-resolve - unchanged current behavior: abort.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os as o\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_rebase_aborts_on_non_import_conflicting_lines(tmp_path, monkeypatch):
    # A conflict on added lines that are NOT import/use statements must never
    # auto-resolve, even though both sides are pure additions.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nx = 1\n\n\ndef foo():\n    pass\n"
    master = "import os\nx = 2\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_rebase_aborts_all_or_nothing_across_multiple_files(tmp_path, monkeypatch):
    # A clean additive-import conflict in one file plus a disqualifying
    # conflict in another must abort the WHOLE rebase - no partial per-file
    # resolution.
    base_a = "import os\n\n\ndef foo():\n    pass\n"
    base_b = "import os\n\n\ndef bar():\n    pass\n"
    agent = {
        "a.py": "import os\nimport sys\n\n\ndef foo():\n    pass\n",
        "b.py": "import os\nimport sys\n\n\ndef bar():\n    pass\n",
    }
    master = {
        "a.py": "import os\nimport json\n\n\ndef foo():\n    pass\n",
        # Same anchor line as agent's edit (right after "import os") so this
        # genuinely conflicts, but it MODIFIES the existing line instead of
        # purely adding one - the disqualifying edit for this file.
        "b.py": "import os as o\n\n\ndef bar():\n    pass\n",
    }
    repo, wt = _setup_conflict_repo(
        tmp_path, {"a.py": base_a, "b.py": base_b}, agent, master,
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_auto_resolve_conflict_disqualifies_on_write_failure(tmp_path, monkeypatch):
    # A write failure (ENOSPC, EROFS, quota, etc.) while applying an
    # otherwise-eligible additive-import resolution must disqualify the step
    # (return []) rather than propagate and leave the worktree mid-rebase.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os\nimport json\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    subprocess.run(["git", "fetch", "origin"], cwd=wt, capture_output=True, text=True, check=True)
    r = subprocess.run(["git", "rebase", "origin/master"], check=False, cwd=wt, capture_output=True, text=True)
    assert r.returncode != 0, "expected the rebase to conflict"

    def _boom(self, *a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = p._try_auto_resolve_conflict(str(wt))
    assert result == []


def test_rebase_no_conflict_has_no_auto_resolved_key(tmp_path, monkeypatch):
    # Regression check: a normal, non-conflicting rebase must keep returning
    # its existing shape - no `auto_resolved` key at all for the common case.
    repo, wt = _setup_conflict_repo(
        tmp_path,
        {"a.py": "x = 1\n", "b.py": "y = 1\n"},
        {"a.py": "x = 1\nx2 = 2\n"},
        {"b.py": "y = 1\ny2 = 2\n"},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True
    assert rb["conflict"] is False
    assert "auto_resolved" not in rb


def test_ci_status_none_when_gh_unavailable(monkeypatch):
    # No PR / no gh -> state "none" is treated as pass so repos without CI are
    # not blocked. A non-zero gh exit (no checks for the branch) maps here too.
    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no checks found"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"


def test_ci_status_fail_on_fail_bucket(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "fail"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "fail"


def test_ci_status_pass_when_all_pass(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "pass"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "pass"


def test_ci_status_pending_times_out(monkeypatch):
    # A check that never reaches a terminal bucket must not hang the tick; it
    # returns "pending" after the timeout so the merge is retried later.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "pending"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda _s: None)
    ci = p._ci_status("agent/x", sha="", timeout_s=0)
    assert ci["state"] == "pending"


def test_ci_status_returns_none_when_gh_missing(monkeypatch):
    # `gh` absent/non-executable raises OSError; the helper must honor its
    # never-raises contract and map that to "none" (treat as pass) rather than
    # escaping and crashing the scheduler tick.
    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"
    assert "gh unavailable" in ci["error"]


def test_ci_status_none_when_no_workflows_dir_and_no_checks_reported(
    monkeypatch, tmp_path,
):
    # A repo with no .github/workflows genuinely has no CI: an empty checks
    # list must resolve straight to "none" (treated as pass), not block a
    # merge waiting for checks that will never appear.
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)

    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"


def test_ci_status_pending_not_none_when_workflows_dir_present_but_checks_not_yet_registered(
    monkeypatch, tmp_path,
):
    # A repo WITH .github/workflows that reports zero checks yet must NOT be
    # treated as pass - the workflow run may just not have registered with
    # GitHub yet. Fix for the gap that let PR #48 merge with a red Linux CI
    # job that hadn't shown up in `gh pr checks` at merge time (2026-07-07
    # web-client-epic retro §4). Must poll (not fast-path) and land on
    # "pending", never silently "none"/pass, once the timeout elapses.
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)

    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda _s: None)
    # A tiny positive timeout (not 0): 0 would let the while loop's deadline
    # already be past on the first condition check, skipping the loop body
    # (and thus the gh call under test) entirely and falling through to
    # "pending" for free - passing even with the pre-fix "none" bug.
    ci = p._ci_status("agent/x", sha="", timeout_s=0.05)
    assert ci["state"] == "pending"


def test_ci_status_cancelled_when_only_cancelled_bucket(monkeypatch):
    # A job cancelled by an abnormal queue delay is a transient event worth
    # one auto-rerun, not a terminal failure - it must be distinguishable
    # from "fail" so callers can retry instead of giving up immediately.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "cancelled"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "cancelled"


def test_ci_status_fail_wins_over_cancelled_when_both_present(monkeypatch):
    # A genuine failure alongside an unrelated cancelled job must still be
    # reported as "fail" - cancelled-only auto-rerun must never mask a real
    # test/lint failure.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "cancelled"}, {"bucket": "fail"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "fail"


def test_ci_rerun_issues_gh_run_rerun_on_success(monkeypatch):
    # SHA-scoped (Mode 26): the run to rerun is looked up by the exact commit
    # SHA via `gh api .../actions/runs?head_sha=`, not by branch name.
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "12345" if argv[:2] == ["gh", "api"] else ""
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_rerun("deadbeef") is True
    assert any(a[:2] == ["gh", "api"] and "head_sha=deadbeef" in a[2] for a in calls)
    assert any(a[:3] == ["gh", "run", "rerun"] and "12345" in a for a in calls)
    assert any("--failed" in a for a in calls)


def test_ci_rerun_returns_false_when_gh_run_list_fails(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no runs found"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_rerun("agent/x") is False


def test_ci_rerun_returns_false_never_raises_when_gh_missing(monkeypatch):
    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    assert p._ci_rerun("agent/x") is False


def test_rebase_onto_master_returns_not_ok_when_git_missing(monkeypatch, tmp_path):
    # `git` absent/non-executable raises OSError; the helper must not escape it
    # (the loop only wraps _merge_pr in try/except). It reports a non-conflict
    # failure so the caller parks rather than crashing.
    wt = tmp_path / "wt"
    wt.mkdir()

    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is False


def test_advance_pipeline_push_failure_blocks_merge(plan_dir, monkeypatch, tmp_path):
    # A failed force-push (lease rejected / network / auth) must block before
    # _ci_status and _merge_pr: otherwise the remote HEAD stays stale and the
    # gate squashes pre-rebase code. It counts against merge_attempts and leaves
    # the story pr_open for retry.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_manifest(plan_dir, "pushfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": str(wt)},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt_, br: {"ok": True, "conflict": False, "error": ""})

    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "non-fast-forward (lease rejected)"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt_, key: merged_calls.append(key))

    result = p.advance_pipeline("pushfail")

    story = _read_manifest(plan_dir, "pushfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []          # _merge_pr must not run on a push fail
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_approve_merge_push_failure_returns_error(plan_dir, monkeypatch, tmp_path):
    # The manual override must also surface a failed force-push rather than
    # proceeding to merge stale remote code.
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_manifest(plan_dir, "ampush", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": str(wt)},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt_, br: {"ok": True, "conflict": False, "error": ""})

    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "lease rejected"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt_, key: merged_calls.append(key))

    result = p.approve_merge("ampush", "P1")

    assert result["ok"] is False
    assert "push failed" in result["error"]
    assert merged_calls == []


def test_advance_pipeline_rebase_conflict_retries_within_budget(plan_dir, monkeypatch):
    # A rebase conflict blocks the merge before _merge_pr is ever called; it
    # counts against merge_attempts and leaves the story pr_open for retry.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rbconflict", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True,
                                        "error": "conflict in app.js"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("rbconflict")

    story = _read_manifest(plan_dir, "rbconflict")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []          # _merge_pr must not run on a rebase fail
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_advance_pipeline_rebase_conflict_exhausts_budget(plan_dir, monkeypatch):
    # Once the budget is spent on repeated rebase conflicts, the story fails
    # for human intervention rather than retrying forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rbgiveup", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 2},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True, "error": "boom"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("rbgiveup")

    story = _read_manifest(plan_dir, "rbgiveup")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 3
    assert "rebase:" in story.get("merge_error", "")
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_blocks_merge(plan_dir, monkeypatch):
    # A failing CI check blocks the merge and counts against the budget; the
    # reviewer APPROVE alone is not sufficient to land a red PR.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "ruff"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("cifail")

    story = _read_manifest(plan_dir, "cifail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []
    assert result["failed"] == []


def test_advance_pipeline_ci_fail_routes_to_rework_when_opted_in(plan_dir, monkeypatch):
    # PIPELINE_REWORK_ON_CI_FAIL=1: a definitive CI test failure on an
    # APPROVEd branch (e.g. the agent's own broken self-test, invisible to
    # the acceptance-scoped reviewer) is handed back to the implementer as
    # rework feedback instead of silently retrying the unchanged branch.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifailrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_clamp_boundary failed"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("cifailrework")

    story = _read_manifest(plan_dir, "cifailrework")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert "test_clamp_boundary failed" in story["review_feedback"]
    assert merged_calls == []
    assert result["merged"] == []
    assert result["failed"] == []


def test_advance_pipeline_ci_fail_rework_sets_ci_rework_flag(plan_dir, monkeypatch):
    # L1 (REVIEWER_ESCALATION_PLAN.md): when a definitive CI failure is routed
    # to rework, the story must carry a `ci_rework` flag so dispatch_story can
    # raise the agent's done-bar to full-suite-green on the redispatch. Without
    # it the rework round keeps the oracle-green bar and re-fails CI on the same
    # assertion (the gpt-oss token_bucket loop).
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cireworkflag", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_x failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    p.advance_pipeline("cireworkflag")

    story = _read_manifest(plan_dir, "cireworkflag")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story.get("ci_rework") is True


def test_advance_pipeline_ci_fail_rework_feedback_uses_wired_helper(plan_dir, monkeypatch):
    # Dead-code/wiring guard (MODE40-CI-REWORK-FEEDBACK-V2): the merge-gate
    # CI-fail block must CALL _ci_rework_feedback(gate_error) rather than keep
    # an inline template. The glm-authored unit suite calls the helper in
    # isolation, so an executor that defines _ci_rework_feedback but skips
    # wiring it into the merge-gate ships DEAD CODE and still passes that
    # suite. This integration test closes that gap: it drives advance_pipeline's
    # real CI-fail -> rework path and asserts review_feedback carries the
    # commit-required sentence the helper ALWAYS appends (both lint and
    # non-lint branches). The pre-MODE40 inline template lacks that sentence
    # (it instead says "an incorrect assertion"), so this assertion fails on
    # an unwired/inline merge-gate and passes only when the helper is wired.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cireworkwired", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_clamp_boundary failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    p.advance_pipeline("cireworkwired")

    story = _read_manifest(plan_dir, "cireworkwired")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    # The helper always appends this commit-required sentence; the old inline
    # template does not. Its presence proves the merge-gate called the helper.
    assert "A NEW COMMIT on your branch is REQUIRED" in story["review_feedback"]
    # And the old inline template's telltale wording must be gone.
    assert "incorrect assertion" not in story["review_feedback"]


def test_advance_pipeline_ci_fail_stays_terminal_without_opt_in(plan_dir, monkeypatch):
    # Without the flag, a definitive CI failure keeps today's exact behavior:
    # merge_attempts increments and the story terminal-fails at the cap - no
    # rework routing.
    monkeypatch.delenv("PIPELINE_REWORK_ON_CI_FAIL", raising=False)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 1)
    _write_manifest(plan_dir, "cifailnorework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "boom"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cifailnorework")

    story = _read_manifest(plan_dir, "cifailnorework")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_rework_exhausted_falls_to_terminal_fail(plan_dir, monkeypatch):
    # Once the merge-CI rework budget is already spent (merge_attempts has
    # reached MERGE_MAX_ATTEMPTS), a further CI fail must not loop forever on
    # rework - it falls through to the existing terminal-fail path.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifailexhausted", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 3},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "still broken"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cifailexhausted")

    story = _read_manifest(plan_dir, "cifailexhausted")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 4  # was 3, +1 in the terminal-fail fall-through
    assert "rework_attempts" not in story
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_rework_counter_survives_review_approve(plan_dir, monkeypatch):
    # Regression (2026-07-17, token_bucket live run): the merge-CI->rework
    # loop MUST be bounded by merge_attempts, not rework_attempts. The review
    # APPROVE path (~line 4189) pops rework_attempts on every pass because the
    # acceptance-scoped reviewer APPROVEs whenever the oracle is green - so a
    # bound on rework_attempts resets to 0 each cycle and the loop never
    # exhausts (observed: four identical "routed to rework (1/3)"
    # notifications, same broken assertion every round). merge_attempts is the
    # merge gate's own counter and is NOT reset by review, so it must advance
    # 1->2->3 across rework -> review APPROVE -> merge-gate cycles, then
    # terminal-fail instead of looping forever.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicycle", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_x failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    def _simulate_rework_then_review_approve():
        # The agent re-dispatched off the rework feedback, the acceptance-
        # scoped reviewer APPROVEd (oracle green), and the review APPROVE
        # path popped rework_attempts. Story returns to pr_open for the
        # merge gate to re-run CI on the next tick.
        st = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
        st["status"] = "pr_open"
        st["review_verdict"] = "APPROVE"
        st.pop("rework_attempts", None)  # what review APPROVE does (~line 4189)
        _write_manifest(plan_dir, "cicycle", {"P1": st})

    # Tick 1: CI fail -> routed to rework, merge_attempts 0->1.
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 1

    # Tick 2: same CI fail after a review APPROVE that reset rework_attempts.
    # The bound must advance to 2/3, NOT reset back to 1/3 (the bug).
    _simulate_rework_then_review_approve()
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 2

    # Tick 3: advances to 3/3 (still within budget, routes once more).
    _simulate_rework_then_review_approve()
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 3

    # Tick 4: budget exhausted (merge_attempts=3 >= MERGE_MAX_ATTEMPTS=3) ->
    # terminal fail, no further rework routing (no infinite loop).
    _simulate_rework_then_review_approve()
    result = p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "failed"
    assert "P1" in result["failed"]


def test_advance_pipeline_transient_push_failure_not_routed_to_rework(plan_dir, monkeypatch):
    # A push/network failure is not a CI verdict at all - it must never
    # consume rework budget even with the opt-in flag set.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipushfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "network down"})(),
    )
    ci_calls = []
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: ci_calls.append(br))
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipushfail")

    story = _read_manifest(plan_dir, "cipushfail")["stories"]["P1"]
    assert ci_calls == []  # push failed before CI was ever consulted
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_ci_pending_not_routed_to_rework(plan_dir, monkeypatch):
    # A pending CI result is not a definitive failure - it must keep retrying
    # via the ordinary merge_attempts path, never rework, even with the flag on.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipendingrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pending", "error": "timeout"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipendingrework")

    story = _read_manifest(plan_dir, "cipendingrework")["stories"]["P1"]
    assert story["status"] == "pr_open"
    # Non-blocking S5 contract: a pending CI result yields the tick (sets
    # ci_pending_since) instead of blocking with a gate_error that would
    # consume a merge attempt. It must never be routed to rework.
    assert story.get("ci_pending_since") is not None
    assert "merge_attempts" not in story
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_cancelled_ci_not_routed_to_rework(plan_dir, monkeypatch):
    # A cancelled-only CI result (after its one auto-rerun) carries no
    # code-quality signal - it must fall to the ordinary retry path, not rework.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicancelrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "ci_rerun_attempted": True},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "cancelled", "error": ""})
    monkeypatch.setattr(p, "_ci_rerun", lambda br: True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cicancelrework")

    story = _read_manifest(plan_dir, "cicancelrework")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_ci_pending_blocks_merge(plan_dir, monkeypatch):
    # Pending CI must not merge yet; it retries within budget instead.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipending", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pending", "error": "timeout"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipending")

    story = _read_manifest(plan_dir, "cipending")["stories"]["P1"]
    assert story["status"] == "pr_open"
    # Non-blocking S5 contract: a pending CI result yields the tick back to the
    # scheduler (sets ci_pending_since) instead of blocking with a gate_error
    # that would consume a merge attempt.
    assert story.get("ci_pending_since") is not None
    assert "merge_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_rebase_and_ci_ok_merges(plan_dir, monkeypatch):
    # The happy path: rebase ok + CI pass -> merge proceeds and clears the
    # attempt counter, exactly like a pre-gate merge.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "rbok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 1},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("rbok")

    story = _read_manifest(plan_dir, "rbok")["stories"]["P1"]
    assert story["status"] == "done"
    assert "merge_attempts" not in story
    assert result["merged"] == ["P1"]


def test_advance_pipeline_ci_gate_disabled_skips_ci(plan_dir, monkeypatch):
    # PIPELINE_MERGE_CI_GATE=0 is the documented opt-out: the real _ci_status
    # short-circuits to pass without ever calling gh, so a rebase-ok branch
    # merges even if checks would have failed. Prove gh is not consulted by
    # making any subprocess.run raise.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(pci, "PIPELINE_MERGE_CI_GATE", False)
    _write_manifest(plan_dir, "cidisabled", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})

    def _boom_run(*a, **k):
        raise AssertionError("subprocess must not run when CI gate is disabled")

    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("cidisabled")

    story = _read_manifest(plan_dir, "cidisabled")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_advance_pipeline_cancelled_ci_triggers_one_rerun_then_merges(plan_dir, monkeypatch):
    # A cancelled-only CI result is worth exactly one automatic rerun before
    # falling back to the ordinary fail/retry path - not an immediate park.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "cicancel", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    ci_calls = []

    def _fake_ci_status(br, **_):
        ci_calls.append(br)
        if len(ci_calls) == 1:
            return {"state": "cancelled", "error": ""}
        return {"state": "pass", "error": ""}

    rerun_calls = []
    monkeypatch.setattr(p, "_ci_status_once", _fake_ci_status)
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("cicancel")

    assert len(rerun_calls) == 1
    assert len(ci_calls) == 2
    story = _read_manifest(plan_dir, "cicancel")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_advance_pipeline_cancelled_ci_second_time_does_not_rerun_again(plan_dir, monkeypatch):
    # ci_rerun_attempted, once set, bounds the auto-rerun to exactly once per
    # story - a second cancelled result must fall straight to the ordinary
    # fail/retry path instead of rerunning indefinitely.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicancel2", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "ci_rerun_attempted": True},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "cancelled", "error": ""})
    rerun_calls = []
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")

    result = p.advance_pipeline("cicancel2")

    assert rerun_calls == []
    story = _read_manifest(plan_dir, "cicancel2")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert result["merged"] == []


def test_approve_merge_cancelled_ci_triggers_one_rerun_then_merges(plan_dir, monkeypatch):
    # Same one-shot auto-rerun behavior on the human-driven approve_merge
    # path as the scheduler's merge gate.
    _write_manifest(plan_dir, "amcancel", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    ci_calls = []

    def _fake_ci_status(br, **_):
        ci_calls.append(br)
        if len(ci_calls) == 1:
            return {"state": "cancelled", "error": ""}
        return {"state": "pass", "error": ""}

    rerun_calls = []
    monkeypatch.setattr(p, "_ci_status", _fake_ci_status)
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("amcancel", "P1")

    assert result["ok"] is True
    assert len(rerun_calls) == 1
    assert len(ci_calls) == 2
    story = _read_manifest(plan_dir, "amcancel")["stories"]["P1"]
    assert story["status"] == "done"


def test_reverify_acceptance_reruns_full_suite_without_acceptance_block(monkeypatch, tmp_path):
    # Gap 1: stories without an acceptance block (the common case for
    # real-project stories) get the rebased branch's full test suite
    # re-run before merge, not a silent "none" pass. The 1 MBW in the
    # gpt-oss + glm-5.2:cloud v2 run was only caught because benchmark
    # stories carry acceptance oracles; a real-project story without
    # one had no second check after rebase until this fix. Behavior
    # change flagged per CLAUDE.md Step 4 (this test replaces the old
    # `test_reverify_acceptance_returns_none_without_acceptance_block`,
    # which asserted the silent-pass path).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result["state"] == "fail"
    assert "failed" in result["error"]
    # The cmd must be the unscoped full suite (no acceptance paths
    # appended), because there is no acceptance block to scope to.
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_passes_when_full_suite_green(monkeypatch, tmp_path):
    # The "no acceptance block" arm returns pass on rc=0, not "none" -
    # the MBW safety net only catches real failures; a clean suite
    # merges normally.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_full_suite_opt_out_restores_none(monkeypatch, tmp_path):
    # `PIPELINE_REVERIFY_FULL_SUITE=0` restores the old silent-pass
    # behavior for operators with slow test suites who don't want a
    # full-suite re-run at the merge gate. Mirrors the opt-out pattern
    # used by `PIPELINE_MERGE_CI_GATE`.
    def _boom_run(*a, **k):
        raise AssertionError("must not run tests when opted out")
    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setenv("PIPELINE_REVERIFY_FULL_SUITE", "0")

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "none", "error": ""}


def test_reverify_acceptance_scopes_cargo_to_acceptance_test_stem(monkeypatch, tmp_path):
    # FM-A fix: a non-pytest runner with an acceptance block is now scoped to
    # the oracle fixture, not run as the full suite. cargo names integration
    # tests by file stem, so tests/acc.rs -> `cargo test --test acc` runs ONLY
    # the oracle, excluding the implementer's own tests/<name>.rs (previously
    # the full `cargo test` graded the implementer's own tests — the
    # "graded on own buggy tests" failure mode).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["cargo", "test"]))

    result = p._reverify_acceptance(
        {"summary": "x", "acceptance": [{"path": "tests/acc.rs", "source": "// x"}]},
        str(tmp_path),
    )

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["cargo", "test", "--test", "acc"]


def test_reverify_acceptance_reruns_full_suite_for_unscopeable_runner(monkeypatch, tmp_path):
    # Safety net preserved: runners we can't safely scope (mvn, gradle, make,
    # jest-style npm) fall back to the full suite, so a post-rebase break in a
    # real-project story without a scoping-safe runner still can't slip through.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["mvn", "test"]))

    result = p._reverify_acceptance(
        {"summary": "x", "acceptance": [{"path": "tests/acc.rs", "source": "// x"}]},
        str(tmp_path),
    )

    assert result == {"state": "pass", "error": ""}
    # mvn can't be safely scoped -> full suite.
    assert seen_cmd["cmd"] == ["mvn", "test"]


def test_reverify_acceptance_appends_own_test_paths_without_acceptance_block(
    monkeypatch, tmp_path,
):
    # Mode 42 done-bar blindspot, merge-gate side: a no-acceptance story
    # whose deliverable lives under tests/ gets its own new tests/test_*.py
    # file appended to the full-suite re-run when story_key is given, so a
    # break the model's own test would have caught can't slip through the
    # last check before merge (see _added_pytest_test_paths).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda wt, key, base: ["tests/benchmark/test_driver.py"]
        if key == "S1" else [],
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == [
        "pytest", str(tmp_path / "tests/benchmark/test_driver.py")]


def test_reverify_acceptance_no_story_key_leaves_full_suite_unscoped(
    monkeypatch, tmp_path,
):
    # Backward-compat default: callers that don't pass story_key (the
    # pre-existing 2-arg call shape) get the plain full suite, unchanged.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called without a story_key")),
    )

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_does_not_augment_when_acceptance_block_present(
    monkeypatch, tmp_path,
):
    # A story WITH an acceptance block stays scoped to the oracle only
    # (FM-A) - own-test-path augmentation is only for the no-acceptance
    # full-suite arm.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called when acceptance block is present")),
    )
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest", str(tmp_path / "test_acceptance.py")]


def test_reverify_acceptance_skips_augmentation_for_non_pytest_runner(
    monkeypatch, tmp_path,
):
    # A non-pytest full-suite command (e.g. mvn) must not have
    # _added_pytest_test_paths' output appended - it only knows how to
    # extend a pytest invocation.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["mvn", "test"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called for a non-pytest runner")),
    )

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["mvn", "test"]


def test_reverify_acceptance_returns_none_for_missing_worktree(monkeypatch):
    def _boom_run(*a, **k):
        raise AssertionError("must not run tests against a missing worktree")

    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, "/no/such/worktree")

    assert result == {"state": "none", "error": ""}


def test_reverify_acceptance_fails_when_oracle_red(monkeypatch, tmp_path):
    # The exact case the RLI-3 merged-but-wrong incident needed caught: the
    # acceptance test references behavior the branch never implemented.
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    seen_cmd = {}

    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="AttributeError: no available_tokens", stderr="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path))

    assert result["state"] == "fail"
    assert "available_tokens" in result["error"]
    assert seen_cmd["cmd"] == ["pytest", str(tmp_path / "test_acceptance.py")]


def test_reverify_acceptance_passes_when_oracle_green(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path))

    assert result == {"state": "pass", "error": ""}


# ---------- _reverify_build (T4) ----------
# Neither the reviewer nor the dispatched agent's own "tests pass" report is
# proof the project actually builds - PR #48 shipped with `npm run build`
# broken (a real, pre-existing bug: Node's `crypto` module can't bundle for a
# browser target) because nobody ran it before merge (2026-07-07
# web-client-epic retro §3.1).

def test_reverify_build_fails_when_build_command_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Error: Can't resolve 'crypto'"),
    )

    result = p._reverify_build(str(tmp_path))

    assert result["state"] == "fail"
    assert "crypto" in result["error"]


def test_reverify_build_passes_when_build_command_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "pass", "error": ""}


def test_reverify_build_skips_when_no_build_command_detected(monkeypatch, tmp_path):
    # A repo without a build step (most real-project stories) must merge
    # freely - "none" is a skip, not a block.
    def _boom_run(*a, **k):
        raise AssertionError("must not run anything when no build command is detected")
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: None)
    monkeypatch.setattr(p.subprocess, "run", _boom_run)

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "none", "error": ""}


def test_reverify_build_returns_none_for_missing_worktree(monkeypatch):
    def _boom_run(*a, **k):
        raise AssertionError("must not attempt a build against a missing worktree")
    monkeypatch.setattr(p.subprocess, "run", _boom_run)

    result = p._reverify_build("/no/such/worktree")

    assert result == {"state": "none", "error": ""}


def test_reverify_build_opt_out_restores_none(monkeypatch, tmp_path):
    # PIPELINE_MERGE_BUILD_GATE=0 restores the old silent-skip behavior for
    # operators with slow builds who don't want a build re-run at the merge
    # gate. Mirrors the opt-out pattern used by PIPELINE_MERGE_CI_GATE and
    # PIPELINE_REVERIFY_FULL_SUITE.
    def _boom_run(*a, **k):
        raise AssertionError("must not build when opted out")
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(pci, "PIPELINE_MERGE_BUILD_GATE", False)

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "none", "error": "build gate disabled"}


def test_advance_pipeline_build_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # A reviewer APPROVE + green CI + passing tests is not sufficient to land
    # a branch whose build, re-run independently right before merge, fails.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "buildfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build",
                        lambda wt: {"state": "fail", "error": "Error: Can't resolve 'crypto'"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("buildfail")

    story = _read_manifest(plan_dir, "buildfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_build_reverify_pass_merges(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "buildok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("buildok")

    story = _read_manifest(plan_dir, "buildok")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_approve_merge_build_reverify_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a branch whose build,
    # re-run independently right before merge, fails.
    _write_manifest(plan_dir, "ambuild", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build",
                        lambda wt: {"state": "fail", "error": "build broke"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("ambuild", "P1")

    assert result["ok"] is False
    assert "build reverify fail" in result["error"]
    assert merged_calls == []


def test_advance_pipeline_acceptance_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # A reviewer APPROVE + green CI is not sufficient to land a branch whose
    # acceptance oracle, re-run independently right before merge, is red.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "accfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "AttributeError"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("accfail")

    story = _read_manifest(plan_dir, "accfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_full_suite_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # Gap 1: the no-acceptance-block path runs the full test suite at the
    # merge gate. A story that broke a sibling's module after rebase is
    # now caught here, even without a harness-owned acceptance oracle.
    # Mirrors the existing `test_advance_pipeline_acceptance_reverify_fail_blocks_merge`
    # but for the no-acceptance case (which is the common one for real
    # projects; the benchmark tasks all carry oracles and were already
    # covered).
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "fsfail", {
        "P1": {"summary": "approved but rebased-broken", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x",
               # NO acceptance block - ordinary TDD story.
               },
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "ModuleNotFoundError: shared"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("fsfail")

    story = _read_manifest(plan_dir, "fsfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_acceptance_reverify_pass_merges(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "accok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("accok")

    story = _read_manifest(plan_dir, "accok")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_approve_merge_acceptance_reverify_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a branch whose acceptance
    # oracle fails on reverification, even with a prior reviewer APPROVE.
    _write_manifest(plan_dir, "amacc", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "AttributeError"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amacc", "P1")

    assert result["ok"] is False
    assert "acceptance reverify fail" in result["error"]
    assert merged_calls == []


def test_approve_merge_rebase_conflict_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a conflicting branch; it
    # surfaces the rebase failure rather than calling _merge_pr.
    _write_manifest(plan_dir, "amrb", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True, "error": "boom"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amrb", "P1")

    assert result["ok"] is False
    assert "rebase failed" in result["error"]
    assert merged_calls == []


def test_approve_merge_ci_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a CI-red PR.
    _write_manifest(plan_dir, "amci", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status",
                        lambda br, **_: {"state": "fail", "error": "ruff"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amci", "P1")

    assert result["ok"] is False
    assert "CI failing" in result["error"]
    assert merged_calls == []


def test_approve_merge_returns_error_on_merge_failure(plan_dir, monkeypatch):
    # The manual override surfaces a merge failure as a structured error to the
    # human invoking it rather than raising an unhandled exception.
    _write_manifest(plan_dir, "ammergefail", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh down")))

    result = p.approve_merge("ammergefail", "P1")

    assert result["ok"] is False
    assert "gh down" in result["error"]
    assert _read_manifest(plan_dir, "ammergefail")["stories"]["P1"]["status"] == "parked"


def test_advance_pipeline_redispatches_changes_requested(plan_dir, monkeypatch):
    # A story the reviewer sent back must be dispatch-eligible so the next tick
    # picks it up and reworks it - otherwise it freezes forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "cr", {
        "S1": {"summary": "rework me", "status": "changes_requested",
               "dependencies": [], "worktree": "/x",
               "review_feedback": "fix the bug", "rework_attempts": 1},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cr")

    assert dispatched == ["S1"]


def test_advance_pipeline_dispatch_failure_retries_within_budget(plan_dir, monkeypatch):
    # A raising dispatch_story (bad git pull, backend hiccup) must not crash the
    # tick: the story keeps its dispatch-eligible status, its attempt counter is
    # bumped, and the user is notified so the next tick retries.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispretry", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("git pull failed")))

    result = p.advance_pipeline("dispretry")

    story = _read_manifest(plan_dir, "dispretry")["stories"]["T1"]
    assert story["status"] == "todo"
    assert story["dispatch_attempts"] == 1
    assert "T1" not in result["failed"]
    assert "T1" in result["notify"]


def test_advance_pipeline_dispatch_failure_exhausts_budget(plan_dir, monkeypatch):
    # A persistently failing launch becomes a hard failure (terminal: failed is
    # not dispatch-eligible) rather than retrying every tick forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispgiveup", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": [],
               "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("dispgiveup")

    story = _read_manifest(plan_dir, "dispgiveup")["stories"]["T1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3
    assert "still broken" in story.get("dispatch_error", "")
    assert "T1" in result["failed"]
    assert "T1" in result["notify"]


def test_check_story_status_failed_launch_exhausts_budget(plan_dir, tmp_path, monkeypatch):
    # An empty agent.log past the startup grace window is a failed launch.
    # Within budget it stays interrupted (redispatched); once the budget is
    # spent it becomes a terminal failure. The grace window protects
    # legitimate-but-slow startups (Ollama -np 1 queueing) from being
    # mis-classified — zero it here so this test still exercises the
    # failed-launch path without racing the wall clock.
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 0)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")
    _write_manifest(plan_dir, "launchgiveup", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("must not run tests")))

    result = p.check_story_status("launchgiveup", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "launchgiveup")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3


def test_check_story_status_successful_run_clears_dispatch_attempts(plan_dir, tmp_path, monkeypatch):
    # Once a launch actually produces output and the tests run, the failed-launch
    # counter is cleared so earlier infra blips don't count against a clean run.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "launchclear", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    class _Done:
        returncode = 0
        stdout = "ok"
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (str(worktree), ["true"]))
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Done())

    result = p.check_story_status("launchclear", "S1")

    assert result["status"] == "tests_passed"
    assert "dispatch_attempts" not in _read_manifest(plan_dir, "launchclear")["stories"]["S1"]


def test_plane_set_state_retries_then_succeeds(monkeypatch):
    # A transient Plane failure is retried within budget rather than dropped.
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")
    calls = []

    def _flaky(method, path, **kw):
        calls.append(path)
        if len(calls) < 2:
            raise RuntimeError("502")
        return {}
    monkeypatch.setattr(pt, "plane_request", _flaky)

    assert p._plane_set_state("S1", "started") is True
    assert len(calls) == 2


def test_plane_set_state_gives_up_after_budget_and_notifies(plan_dir, monkeypatch):
    # A persistent Plane outage gives up after the budget WITHOUT raising (Plane
    # is best-effort) and records the drop durably instead of a silent print.
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plane down")))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p._plane_set_state("S1", "completed", plan_name="pl")

    assert result is False
    assert len(notes) == 1
    assert "plane down" in notes[0]


def test_count_in_progress_agents_counts_across_plans(plan_dir, monkeypatch):
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cnt1", {
        "A1": {"summary": "a", "status": "in_progress", "pid": 1},
        "A2": {"summary": "b", "status": "in_progress", "pid": 2},
        "A3": {"summary": "c", "status": "todo"},
    })
    _write_manifest(plan_dir, "cnt2", {
        "B1": {"summary": "d", "status": "in_progress", "pid": 3},
        "B2": {"summary": "e", "status": "done"},
    })
    assert p._count_in_progress_agents() == 3


def test_count_in_progress_agents_ignores_status_without_pid(plan_dir):
    # A story can be marked in_progress by mark_story_in_progress without
    # ever having been dispatched (no pid) - must not count as a running agent.
    _write_manifest(plan_dir, "cnt3", {
        "A1": {"summary": "a", "status": "in_progress"},
    })
    assert p._count_in_progress_agents() == 0


def test_count_in_progress_agents_skips_dead_pids(plan_dir, monkeypatch):
    # A story can be stuck at in_progress with a pid whose process already
    # exited (e.g. another plan whose own advance_pipeline tick never ran
    # again to notice) - the count must not include it, otherwise it would
    # permanently consume a concurrency slot. The reap itself is a separate
    # step (see test_reap_zombie_in_progress_stories below).
    _write_manifest(plan_dir, "cnt4", {
        "A1": {"summary": "alive", "status": "in_progress", "pid": 111},
        "A2": {"summary": "dead", "status": "in_progress", "pid": 222},
    })

    def _fake_kill(pid, sig):
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    assert p._count_in_progress_agents() == 1


def test_reap_zombie_in_progress_stories(plan_dir, monkeypatch):
    """The reap helper reaps in_progress-with-dead-pid stories back to todo,
    drops their pid, and writes the manifest back to disk. Idempotent: a
    second call on a clean manifest reaps 0.
    """
    _write_manifest(plan_dir, "zap", {
        "Z1": {"summary": "alive", "status": "in_progress", "pid": 111},
        "Z2": {"summary": "dead", "status": "in_progress", "pid": 222},
        "Z3": {"summary": "todo-already", "status": "todo"},
        "Z4": {"summary": "in-progress-no-pid", "status": "in_progress"},
    })

    def _fake_kill(pid, sig):
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    reaped = p._reap_zombie_in_progress_stories()
    assert reaped == 1, "only Z2 (dead pid) should be reaped"
    after = json.loads((plan_dir / "zap.manifest.json").read_text())
    assert after["stories"]["Z1"] == {"summary": "alive", "status": "in_progress", "pid": 111}
    assert after["stories"]["Z2"] == {"summary": "dead", "status": "todo"}
    assert after["stories"]["Z3"] == {"summary": "todo-already", "status": "todo"}
    # Z4 had no pid → not a zombie (just status drift), not reaped.
    assert after["stories"]["Z4"] == {"summary": "in-progress-no-pid", "status": "in_progress"}

    # Second call is idempotent.
    assert p._reap_zombie_in_progress_stories() == 0
    # Manifest unchanged on the no-op reap (no write).
    after2 = json.loads((plan_dir / "zap.manifest.json").read_text())
    assert after == after2


def test_advance_all_plans_does_not_pre_reap_zombies(plan_dir, monkeypatch):
    """Regression guard (2026-06-28): running an external reap pass before
    the polling phase silently leaves dead-pid stories re-dispatching
    forever — the polling phase never sees them, so dispatch_attempts is
    never bumped and the test is never run. advance_all_plans must NOT
    pre-reap; the polling phase handles dead pids via check_story_status
    which falls through to test-running on a dead pid.
    """
    _write_manifest(plan_dir, "zom", {
        "Z1": {"summary": "dead", "status": "in_progress", "pid": 999},
    })

    def _fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    # Stub advance_pipeline to verify it sees the manifest BEFORE any reap.
    captured = {}

    def _fake_advance(plan_name):
        captured.setdefault("calls", []).append(plan_name)
        manifest = json.loads((plan_dir / "zom.manifest.json").read_text())
        captured.setdefault("saw_z1", []).append(
            manifest["stories"]["Z1"]["status"]
        )
        return {"ok": True, "stub": True}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p.advance_all_plans()
    assert "zom" in result["plans"]
    # advance_pipeline must have seen Z1 as in_progress (not pre-reaped to todo)
    # so its polling phase can run check_story_status and bump dispatch_attempts.
    assert captured["saw_z1"] == ["in_progress"]
    # And there's no "reaped_zombies" in the response — reap is no longer
    # wired into advance_all_plans.
    assert "reaped_zombies" not in result


def test_advance_pipeline_caps_dispatch_at_max_concurrent_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    _write_manifest(plan_dir, "cap1", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("cap1")
    assert dispatched == ["T1", "T2"]
    assert result["dispatched"] == ["T1", "T2"]


def test_advance_pipeline_cap_accounts_for_already_running_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cap2", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )

    result = p.advance_pipeline("cap2")
    assert dispatched == ["T1"]
    assert result["dispatched"] == ["T1"]


def test_advance_pipeline_zero_max_concurrent_agents_means_unlimited(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 0)
    _write_manifest(plan_dir, "cap3", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cap3")
    assert dispatched == ["T1", "T2", "T3"]


def test_advance_pipeline_not_paused_redispatches_interrupted_stories(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 20, "week_pct": 10, "paused": False}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "resume", {
        "S1": {"summary": "interrupted one", "status": "interrupted",
               "worktree": "/x", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("resume")
    assert dispatched == ["S1"]


def test_advance_pipeline_plan_paused_skips_dispatch_review_and_merge(plan_dir, monkeypatch):
    # A plan-level pause (pause_plan) must stop a plan from being advanced
    # at all -- unlike the usage gate, it does not even adjudicate merges,
    # since the human asked for this specific plan to stop moving.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    (plan_dir / "halted.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {
            "T1": {"summary": "todo", "status": "todo", "dependencies": []},
            "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
            "TP1": {"summary": "awaiting review", "status": "tests_passed",
                    "worktree": "/y", "risk": "low"},
            "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
                   "risk": "low", "worktree": "/z"},
        },
    }))

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("halted")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []
    assert reviewed == []
    assert merged == []
    assert interrupted == ["R1"]


def test_advance_pipeline_plan_paused_with_no_running_story_is_a_pure_noop(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    (plan_dir / "halted2.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("halted2")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []


def test_pause_plan_sets_manifest_flag(plan_dir):
    _write_manifest(plan_dir, "tobehalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.pause_plan("tobehalted")

    assert result == {"ok": True, "plan_name": "tobehalted", "paused": True}
    assert _read_manifest(plan_dir, "tobehalted")["paused"] is True


def test_resume_plan_clears_manifest_flag(plan_dir):
    (plan_dir / "halted3.manifest.json").write_text(json.dumps({
        "epics": {}, "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))

    result = p.resume_plan("halted3")

    assert result == {"ok": True, "plan_name": "halted3", "paused": False}
    assert _read_manifest(plan_dir, "halted3")["paused"] is False


def test_pause_plan_no_such_manifest_returns_error(plan_dir):
    result = p.pause_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_no_such_manifest_returns_error(plan_dir):
    result = p.resume_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_when_not_paused_is_a_noop(plan_dir):
    _write_manifest(plan_dir, "neverhalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.resume_plan("neverhalted")

    assert result == {"ok": True, "plan_name": "neverhalted", "paused": False}
    assert _read_manifest(plan_dir, "neverhalted")["paused"] is False


def test_advance_all_plans_runs_every_manifest(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True, "plan": plan_name},
    )

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert sorted(calls) == ["p1", "p2"]
    assert result["plans"]["p1"]["ok"] is True
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_isolates_failures_and_continues(plan_dir, monkeypatch):
    """One plan crashing (e.g. a bad repo_root, a missing dependency tool)
    must not abort the whole batch -- other plans still need their tick."""
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    def _fake_advance(plan_name):
        if plan_name == "p1":
            raise RuntimeError("boom")
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert result["plans"]["p1"]["ok"] is False
    assert "boom" in result["plans"]["p1"]["error"]
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_with_no_manifests_returns_empty(plan_dir):
    result = p.advance_all_plans()
    assert result == {"ok": True, "plans": {}}


def test_advance_all_plans_ignores_unignested_plan_json(plan_dir, monkeypatch):
    (plan_dir / "p3.json").write_text(json.dumps({"epics": []}))

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )

    result = p.advance_all_plans()
    assert calls == []
    assert result["plans"] == {}


def test_check_story_status_passing_tests_is_not_done(plan_dir, monkeypatch):
    """"done" must mean merged. A story whose tests just passed is only
    ready for review — conflating the two lets it both skip review (never
    retried, since advance_pipeline only re-checks "in_progress" stories)
    and falsely satisfy other stories' dependency gate before it merges."""
    _write_manifest(plan_dir, "cs", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(plan_dir / "wt")},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    # Pretend the agent wrote real commits — this test is about the
    # `tests_passed` vs `done` distinction, not the empty-branch gate
    # (covered separately by test_check_story_status_*_no_commits).
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("cs", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "cs")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


def test_check_story_status_fails_when_agent_made_no_commits(
    plan_dir, monkeypatch,
):
    """The empty-branch guard: tests passing against an untouched
    worktree (e.g. main's suite against an empty branch because devstral
    parked in a repetition loop without writing code) is NOT the task
    being done. Mark `failed`, not `tests_passed`, so the dashboard
    doesn't count empty branches as success."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "fp", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("fp", "S1")
    assert result["status"] == "failed"
    assert result["reason"] == "empty_agent_branch"
    manifest = _read_manifest(plan_dir, "fp")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "no new commits" in story["failure_reason"]


# ---------- give-up classification (T6) ----------
# The WASM prekey/session story's second attempt (gpt-oss:20b, after being
# split into a smaller story) called `done` with "I'm sorry, I can't
# complete this task" after real research and zero commits (2026-07-07
# web-client-epic retro §3.2). local_agent.py's `done` tool prints its
# summary verbatim as "[step N] DONE: <summary>" - that's the concrete,
# real signal these tests key on, not a fictitious exit protocol.

def test_last_done_summary_extracts_final_done_line(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "[step 1] bash: ls\n"
        "[step 2] DONE: implemented the feature, tests pass\n"
    )
    assert p._last_done_summary(log) == "implemented the feature, tests pass"


def test_last_done_summary_uses_last_done_line_not_first(tmp_path):
    # A resumed agent appends to the same log across ticks; only the LAST
    # DONE line reflects the current run (mirrors _last_nonempty_line's
    # resumed-log caution for STEP_CAP_MARKERS).
    log = tmp_path / "agent.log"
    log.write_text(
        "[step 2] DONE: first attempt summary\n"
        "=== resumed ===\n"
        "[step 5] DONE: second attempt summary\n"
    )
    assert p._last_done_summary(log) == "second attempt summary"


def test_last_done_summary_empty_when_no_done_line(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("[step 1] bash: ls\n[ended without done — step cap reached]\n")
    assert p._last_done_summary(log) == ""


def test_last_done_summary_empty_when_log_missing(tmp_path):
    assert p._last_done_summary(tmp_path / "no-such-log.log") == ""


def test_is_give_up_summary_matches_explicit_surrender():
    assert p._is_give_up_summary("I'm sorry, I can't complete this task.") is True


def test_is_give_up_summary_is_case_insensitive():
    assert p._is_give_up_summary("I CANNOT COMPLETE THIS TASK after research") is True


def test_is_give_up_summary_does_not_match_genuine_completion():
    assert p._is_give_up_summary("implemented the feature, all tests pass") is False


def test_check_story_status_marks_failure_kind_give_up(plan_dir, monkeypatch):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 1] bash: grep -r PreKeyBundle .\n"
        "[step 9] DONE: I'm sorry, I can't complete this task.\n"
    )
    _write_manifest(plan_dir, "giveup1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="1 failed"))

    result = p.check_story_status("giveup1", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "giveup1")
    assert manifest["stories"]["S1"]["failure_kind"] == "give_up"


def test_check_story_status_ordinary_failure_has_no_failure_kind(plan_dir, monkeypatch):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] DONE: implemented the feature per the spec\n"
    )
    _write_manifest(plan_dir, "giveup2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="1 failed"))

    result = p.check_story_status("giveup2", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "giveup2")
    assert "failure_kind" not in manifest["stories"]["S1"]


def test_check_story_status_passes_when_agent_committed_changes(
    plan_dir, monkeypatch,
):
    """Positive case for the empty-branch guard: tests pass AND the
    agent wrote real commits -> status `tests_passed`."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "ok", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("ok", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "ok")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"
    assert "failure_reason" not in manifest["stories"]["S1"]


def test_check_story_status_records_last_test_check_on_pass(plan_dir, monkeypatch):
    """Diagnostic gap found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD):
    check_story_status's test-run result (command, cwd, returncode, output)
    was only ever returned transiently from the tool call - nothing persisted
    it to the manifest, so a status that later turned out to be wrong
    (tests_passed recorded when the same command deterministically fails when
    re-run by hand) was impossible to diagnose after the fact. Persist it on
    the story as `last_test_check` every time a test run determines status,
    regardless of pass/fail, so a future occurrence has a paper trail."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout="3 passed", stderr="",
        ),
    )

    result = p.check_story_status("diag1", "S1")
    assert result["status"] == "tests_passed"

    manifest = _read_manifest(plan_dir, "diag1")
    check = manifest["stories"]["S1"]["last_test_check"]
    assert check["cmd"] == ["pytest", "-q"]
    assert check["cwd"] == str(worktree)
    assert check["returncode"] == 0
    assert "3 passed" in check["stdout_tail"]
    assert "ts" in check


def test_check_story_status_gate_appends_own_new_tests_under_tests_dir(
    plan_dir, monkeypatch,
):
    """Mode 42 done-bar blindspot: a no-acceptance story whose deliverable
    lives under tests/ (e.g. tests/benchmark/run_real_repo_task.py) can add
    its own tests/test_*.py file there, but detect_test_command's
    --ignore=tests then hides that file from THIS SAME gate run - so a
    broken implementation can pass its own (never-executed) test and land
    tests_passed. check_story_status must pass the story's own new/modified
    tests/test_*.py paths explicitly so they actually run (see
    _added_pytest_test_paths)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (wt, ["pytest", "--ignore=tests"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        p, "_added_pytest_test_paths",
        lambda wt, key, base: ["tests/benchmark/test_driver.py"]
        if key == "S1" and base == "main" else [],
    )
    # Capture EVERY subprocess.run call, not just the last: the dead-code
    # gate (which runs after tests pass) also shells out to git, so "the
    # last call" is no longer reliably the test command. The test command
    # is always the first call in check_story_status's flow.
    seen_calls = []
    def _fake_run(cmd, **kwargs):
        seen_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="4 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("diag2", "S1")

    assert result["status"] == "tests_passed"
    assert seen_calls[0] == [
        "pytest", "--ignore=tests", str(worktree / "tests/benchmark/test_driver.py")]


def test_check_story_status_gate_skips_own_test_append_with_acceptance_block(
    plan_dir, monkeypatch,
):
    """A story WITH an acceptance block stays scoped to the harness-owned
    oracle only (FM-A) - the own-new-tests augmentation must not fire, since
    that would grade the story on the model's own (possibly buggy)
    assertions instead of the oracle."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag3", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree),
               "acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (wt, ["pytest", "--ignore=tests"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(
        p, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called when acceptance block is present")),
    )
    # Capture EVERY subprocess.run call, not just the last: the dead-code
    # gate (which runs after tests pass) also shells out to git, so "the
    # last call" is no longer reliably the test command. The test command
    # is always the first call in check_story_status's flow.
    seen_calls = []
    def _fake_run(cmd, **kwargs):
        seen_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("diag3", "S1")

    assert result["status"] == "tests_passed"
    assert seen_calls[0] == [
        "pytest", "--ignore=tests", str(worktree / "test_acceptance.py")]


def test_check_story_status_records_last_test_check_on_fail_without_stderr_attr(
    plan_dir, monkeypatch,
):
    """Same as above, but on the failure path, and with a test double that
    doesn't define .stderr at all (mirrors this file's own `Result` stub
    class used elsewhere) - the diagnostic capture must not crash when the
    subprocess result lacks a stderr attribute."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "1 failed"
        returncode = 1

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("diag2", "S1")
    assert result["status"] == "failed"

    manifest = _read_manifest(plan_dir, "diag2")
    check = manifest["stories"]["S1"]["last_test_check"]
    assert check["returncode"] == 1
    assert "1 failed" in check["stdout_tail"]
    assert check["stderr_tail"] == ""


def test_check_story_status_handles_git_error_safely(plan_dir, monkeypatch):
    """If `_worktree_has_new_commits` returns False (covers the
    `git log` failure case — broken worktree, missing branch, any git
    hiccup), the gate fires: status `failed`, reason
    `empty_agent_branch`. We never crash the orchestrator on a git
    error, and we never accidentally pass a story because git was
    broken."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("ok\n")
    _write_manifest(plan_dir, "broken", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    # Simulate git log returning non-zero (helper returns False).
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    # Must not raise. Must mark failed, not tests_passed.
    result = p.check_story_status("broken", "S1")
    assert result["status"] == "failed"
    assert result["reason"] == "empty_agent_branch"


def _css_setup(plan_dir, monkeypatch, *, last_reviewed_sha=None,
               head_sha=None, rework_attempts=0, acceptance=False):
    """Shared scaffolding for the Mode 27 no-new-commit guard tests.

    Builds a worktree + manifest, mocks the pid dead (so check_story_status
    runs the tests), mocks test detection + new-commits guard, and routes
    subprocess.run so `git rev-parse HEAD` returns `head_sha` while every
    other call (the test command) succeeds with returncode 0.
    """
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("ok\n")
    story = {"summary": "thing", "status": "in_progress", "pid": 4242,
            "worktree": str(worktree), "rework_attempts": rework_attempts}
    if acceptance:
        story["acceptance"] = [{"path": "t.py", "source": ""}]
    if last_reviewed_sha is not None:
        story["last_reviewed_sha"] = last_reviewed_sha
    _write_manifest(plan_dir, "plan", {"S1": story})
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return Result(stdout=(head_sha or "") + "\n")
        return Result(returncode=0)

    monkeypatch.setattr(p.subprocess, "run", run_mock)


def test_check_story_status_no_new_commit_since_last_review_routes_to_changes_requested(
    plan_dir, monkeypatch,
):
    """Mode 27: tests pass but HEAD is unchanged since the last
    REQUEST_CHANGES — route to changes_requested (dispatch-eligible) so the
    scheduler redispatches, instead of stalling at tests_passed where Mode
    24's same-SHA skip guard would loop forever. The no-progress retry
    counts against the rework cap."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=0)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "changes_requested"
    assert manifest["rework_attempts"] == 1


def test_check_story_status_new_commit_after_last_review_passes(plan_dir, monkeypatch):
    """Mode 27: when the rework DID produce a new commit (HEAD advanced past
    last_reviewed_sha), fall through to tests_passed so review_story runs on
    the new SHA — the guard must not fire on legitimate progress."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="def456", rework_attempts=1)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "tests_passed"
    # rework_attempts untouched on the progress path.
    assert manifest["rework_attempts"] == 1


def test_check_story_status_no_last_reviewed_sha_passes(plan_dir, monkeypatch):
    """Mode 27: the guard only applies when a prior REQUEST_CHANGES recorded
    a last_reviewed_sha. A first-run story with no prior review falls
    through to tests_passed unchanged."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha=None, head_sha="any")
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "tests_passed"


def test_check_story_status_no_progress_exhausts_rework_cap_parks(plan_dir, monkeypatch):
    """Mode 27: a stuck agent that keeps producing no new commit must park
    once rework_attempts reaches the cap, rather than redispatching forever.
    rework_attempts starts at 2 (cap 3): one no-progress retry hits the cap
    and parks."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "parked"
    assert manifest["rework_attempts"] == 3
    assert "no new commit after 3" in manifest["parked_reason"]


def test_check_story_status_no_progress_exhausts_rework_cap_escalates_to_claude(
    plan_dir, monkeypatch,
):
    """Root-caused live 2026-07-24 (RUFF-016-ADOPTION, MODE40-CI-REWORK-
    FEEDBACK-V2): review_story's three park paths all escalate to Claude
    under PIPELINE_BACKEND_DISPATCH=auto before parking for a human - this
    was the one rework-exhaustion park path in the file missing that hook,
    so a story that hit exactly this "no new commit" guard never got a
    chance at Claude even with auto-escalation enabled. Same cap/inputs as
    test_check_story_status_no_progress_exhausts_rework_cap_parks, but with
    auto-escalation on: must escalate (backend -> claude, escalated=True,
    status -> changes_requested for redispatch) instead of parking."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_escalated_to_claude"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "changes_requested"
    assert manifest["backend"] == "claude"
    assert manifest["escalated"] is True
    # A fresh rework budget for Claude - _escalate_review_to_claude clears
    # the counter, same as its other two call sites.
    assert "rework_attempts" not in manifest
    # A story that has ALREADY been escalated must terminally park on a
    # second rework-cap exhaustion, not escalate again or loop forever -
    # there is no further fallback past Claude.


def test_check_story_status_no_progress_already_escalated_parks_not_loops(
    plan_dir, monkeypatch,
):
    """The escalated=True guard: a story already on Claude that STILL hits
    the no-new-commit rework cap a second time must park for a human, not
    re-escalate (there's nothing past Claude to fall back to)."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    manifest = _read_manifest(plan_dir, "plan")
    manifest["stories"]["S1"]["escalated"] = True
    manifest["stories"]["S1"]["backend"] = "claude"
    manifest_path = plan_dir / "plan.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "parked"
    assert manifest["backend"] == "claude"


def test_check_story_status_routes_acceptance_fail_to_review_when_opted_in(
    plan_dir, monkeypatch,
):
    """PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1: a dispatch whose acceptance oracle
    FAILED but which produced real work (new commits on the agent branch) is
    routed to review instead of straight to "failed", so the reviewer sees the
    failing submission and the rework loop re-dispatches the model. Without
    this routing every acceptance-failing cell parked at "failed" before
    reaching review, so the configured rework budget and reviewer never ran
    (observed live: 0/9 mlx cells reached review, zero GLM reviewer usage)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "revfail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1   # acceptance oracle FAILED
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail", "S1")

    # Routed to reviewable state, NOT terminal "failed".
    assert result["status"] == "tests_passed"
    assert result["tests_passed"] is False
    story = _read_manifest(plan_dir, "revfail")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["acceptance_failed_review"] is True


def test_check_story_status_acceptance_fail_stays_failed_without_opt_in(
    plan_dir, monkeypatch,
):
    """Without PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL the default behavior is
    unchanged: a failing-acceptance dispatch with real work lands at terminal
    "failed" (no review, no rework)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "nofail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.delenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", raising=False)

    result = p.check_story_status("nofail", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "nofail")["stories"]["S1"]
    assert story["status"] == "failed"
    assert "acceptance_failed_review" not in story


def test_check_story_status_acceptance_fail_stays_failed_for_empty_branch(
    plan_dir, monkeypatch,
):
    """Even with the opt-in set, a failing-acceptance dispatch with NO new
    commits (agent parked without writing code) stays "failed" — re-dispatching
    the same stuck prompt to the same model won't help, so it is not worth a
    review round-trip."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent looped without writing\n")
    _write_manifest(plan_dir, "emptyfail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class _Fail:
        returncode = 1
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("emptyfail", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "emptyfail")["stories"]["S1"]
    assert story["status"] == "failed"
    assert "acceptance_failed_review" not in story


def test_check_story_status_acceptance_fail_review_no_new_commit_routes_to_changes_requested(
    plan_dir, monkeypatch,
):
    """Mode 27 twin: the acceptance-fail-review opt-in (PIPELINE_REVIEW_ON_
    ACCEPTANCE_FAIL=1) routes a failing-tests-but-real-work dispatch to
    tests_passed/acceptance_failed_review — but if HEAD is unchanged since
    the last REQUEST_CHANGES (the rework redispatch crashed, e.g. on an LLM
    transport error, before writing any fix), review_story's same-SHA skip
    guard would silently decline to re-review forever, stranding the story
    at tests_passed (not dispatch-eligible). Observed live 2026-07-20: 14+
    consecutive silent skip-notifications on one story. The original Mode 27
    guard only checked `passed` (True) before this opt-in branch existed as
    a second way to reach tests_passed; it must also cover this path."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work but crashed\n")
    _write_manifest(plan_dir, "revfail_stuck", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "rework_attempts": 1,
               "last_reviewed_sha": "34a3f38"},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    class _RevParse:
        returncode = 0
        stdout = "34a3f38\n"

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return _RevParse()
        return _Fail()
    monkeypatch.setattr(p.subprocess, "run", run_mock)

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail_stuck", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    story = _read_manifest(plan_dir, "revfail_stuck")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 2


def test_check_story_status_acceptance_fail_review_new_commit_still_passes(
    plan_dir, monkeypatch,
):
    """Sibling regression guard: when the acceptance-fail-review opt-in
    fires AND HEAD legitimately advanced past last_reviewed_sha, the guard
    above must not fire — the story reaches tests_passed/
    acceptance_failed_review as before so review_story evaluates the new
    commit."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "revfail_progress", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "rework_attempts": 1,
               "last_reviewed_sha": "34a3f38"},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    class _RevParse:
        returncode = 0
        stdout = "def456\n"

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return _RevParse()
        return _Fail()
    monkeypatch.setattr(p.subprocess, "run", run_mock)

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail_progress", "S1")

    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "revfail_progress")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["acceptance_failed_review"] is True
    assert story["rework_attempts"] == 1


# ---------- reset_false_positive_tests_passed.py unit tests ----------

_RESET_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "reset_false_positive_tests_passed.py"
_reset_spec = importlib.util.spec_from_file_location(
    "reset_false_positive_tests_passed", _RESET_SCRIPT_PATH,
)
reset_script = importlib.util.module_from_spec(_reset_spec)
_reset_spec.loader.exec_module(reset_script)
sys.modules["reset_false_positive_tests_passed"] = reset_script


def _make_story(**overrides) -> dict:
    """A canonical tests_passed story with full review/dispatch bookkeeping.

    Used as the starting point for reset-script tests; override fields
    to construct variations (e.g. cleared fields, status='interrupted')."""
    base = {
        "summary": "thing",
        "agent_instructions": "",
        "dependencies": [],
        "persona": None,
        "model": None,
        "risk": "low",
        "status": "tests_passed",
        "backend": "local",
        "pid": 4242,
        "worktree": "/tmp/wt",
        "log": "/tmp/wt/agent.log",
        "review_verdict": "APPROVE",
        "review_feedback": "looks good",
        "rework_attempts": 2,
        "failure_reason": "old failure",
        "dispatch_attempts": 1,
    }
    base.update(overrides)
    return base


def test_reset_script_resets_empty_branch_false_positive(monkeypatch):
    """A story at tests_passed with no commits on its agent branch is
    the false-positive signature -> reset to interrupted and clear the
    review/dispatch bookkeeping."""
    manifest = {"stories": {"S1": _make_story()}}
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: False)

    action = reset_script.reset_story(manifest, "S1")

    assert action.startswith("RESET")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    # Cleared fields:
    for field in ("review_verdict", "review_feedback", "rework_attempts",
                  "pid", "dispatch_attempts", "failure_reason"):
        assert field not in story, f"{field} should have been cleared"
    # Preserved fields (reused by dispatch_story's resume logic):
    assert story["worktree"] == "/tmp/wt"
    assert story["log"] == "/tmp/wt/agent.log"
    assert story["backend"] == "local"


def test_reset_script_skips_story_with_real_commits(monkeypatch):
    """A tests_passed story whose agent branch has real commits is a
    genuine success, not a false positive. Skip it; don't touch the
    manifest entry."""
    manifest = {"stories": {"S1": _make_story()}}
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: True)

    action = reset_script.reset_story(manifest, "S1")

    assert action.startswith("SKIP")
    story = manifest["stories"]["S1"]
    # Untouched:
    assert story["status"] == "tests_passed"
    assert story["review_verdict"] == "APPROVE"
    assert story["rework_attempts"] == 2
    assert story["pid"] == 4242


def test_reset_script_skips_non_tests_passed_stories(monkeypatch):
    """The script's signature check is `status == tests_passed`. Any
    other status (todo, in_progress, failed, parked, done, interrupted)
    is skipped without inspecting the worktree. This makes the script
    idempotent and safe to re-run after the gate marks a story failed.
    """
    manifest = {
        "stories": {
            "INTERRUPTED": _make_story(status="interrupted"),
            "FAILED":      _make_story(status="failed"),
            "TODO":        _make_story(status="todo"),
            "IN_PROGRESS": _make_story(status="in_progress", pid=9999),
        }
    }
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: False)

    for key in ("INTERRUPTED", "FAILED", "TODO", "IN_PROGRESS"):
        action = reset_script.reset_story(manifest, key)
        assert action.startswith("SKIP"), f"{key}: {action}"
        assert manifest["stories"][key]["status"] != "interrupted" or key == "INTERRUPTED"
    # In particular, IN_PROGRESS still has its pid (would have been
    # cleared if the script had wrongly fired).
    assert manifest["stories"]["IN_PROGRESS"]["pid"] == 9999


def test_reset_script_skips_missing_or_worktree_less_story():
    """A target UUID that isn't in the manifest, or has no recorded
    worktree path, is skipped without raising."""
    manifest_empty = {"stories": {}}
    assert reset_script.reset_story(manifest_empty, "GHOST").startswith("SKIP")

    manifest_no_wt = {"stories": {"S1": _make_story()}}
    manifest_no_wt["stories"]["S1"].pop("worktree")
    assert reset_script.reset_story(manifest_no_wt, "S1").startswith("SKIP")


def test_reset_script_main_writes_manifest_and_reports(monkeypatch, tmp_path):
    """End-to-end of `main()`: builds a tmp manifest with one
    false-positive and one legit tests_passed, runs main() against it,
    asserts only the false-positive was reset and the file was
    written atomically (tmp + rename). The PLAN_DIR/MANIFEST_PATH and
    TARGETS are redirected to test-local values so we don't touch the
    real manifest or depend on the production UUIDs."""
    manifest_path = tmp_path / "e2e-decentralized-messaging-roadmap.manifest.json"
    manifest_path.write_text(json.dumps({
        "stories": {
            "FP":  _make_story(summary="false positive"),
            "REAL": _make_story(summary="real success"),
        }
    }))
    monkeypatch.setattr(reset_script, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(reset_script, "TARGETS", ["FP", "REAL"])
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    # FP has empty branch; REAL has real commits.
    _has_commits = {"FP": False, "REAL": True}

    def _stub(worktree, story_key, base_branch):
        return _has_commits.get(story_key, False)
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits", _stub)

    rc = reset_script.main()

    assert rc == 0
    written = json.loads(manifest_path.read_text())
    assert written["stories"]["FP"]["status"] == "interrupted"
    assert written["stories"]["FP"]["worktree"] == "/tmp/wt"
    assert "review_verdict" not in written["stories"]["FP"]
    # REAL untouched.
    assert written["stories"]["REAL"]["status"] == "tests_passed"
    assert written["stories"]["REAL"]["review_verdict"] == "APPROVE"


def test_reset_script_main_is_noop_when_nothing_matches(monkeypatch, tmp_path):
    """If no target UUID matches the false-positive signature (e.g.
    all have been reset or never were false-positives), main() must
    NOT write the manifest — that would be a needless disk churn and
    would also bump the manifest's mtime, which the orchestrator
    relies on for change detection."""
    manifest_path = tmp_path / "e2e-decentralized-messaging-roadmap.manifest.json"
    original = {"stories": {"S1": _make_story()}}
    manifest_path.write_text(json.dumps(original))
    monkeypatch.setattr(reset_script, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(reset_script, "TARGETS", ["S1"])
    monkeypatch.setattr(reset_script.p, "_default_branch", lambda: "main")
    monkeypatch.setattr(reset_script.p, "_worktree_has_new_commits",
                        lambda *a, **k: True)  # everything is "real"

    rc = reset_script.main()

    assert rc == 0
    # File untouched: same content, no temp file left behind.
    assert json.loads(manifest_path.read_text()) == original
    assert not manifest_path.with_suffix(".json.tmp").exists()

def test_check_story_status_treats_empty_agent_log_as_infra_failure(
    plan_dir, tmp_path, monkeypatch,
):
    """A 0-byte agent.log past the startup grace window — after the process
    has exited — means the headless agent never produced any output, almost
    certainly a failed launch, not a real attempt at the story. Running the
    test suite against the untouched worktree in that case just records a
    misleading "failed" for work that was never tried, and (unlike
    "failed") nothing ever retries it. Treat it like "interrupted" instead,
    which advance_pipeline already redispatches automatically."""
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 0)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")
    _write_manifest(plan_dir, "es", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fail_if_called(*a, **k):
        raise AssertionError("test command should not run against an untouched worktree")
    monkeypatch.setattr(p, "detect_test_command", _fail_if_called)

    result = p.check_story_status("es", "S1")

    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "es")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_check_story_status_runs_tests_normally_when_agent_log_has_content(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Implemented the thing.\nCommitted.\n")
    _write_manifest(plan_dir, "ns", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["false"]))

    class Result:
        stdout = "1 test failed"
        returncode = 1

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("ns", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "ns")
    assert manifest["stories"]["S1"]["status"] == "failed"


def test_checkpoint_commits_and_records_journal_entry(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "abc123\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint(
        "ck", "S1", "step-1", "Implemented the parser",
        next_hint="write tests for edge cases",
    )

    assert result["ok"] is True
    assert result["commit"] == "abc123"
    assert result["step"] == "step-1"

    assert ["git", "add", "-A"] in calls
    assert ["git", "reset", "-q", "--", "agent.log"] in calls
    commit_calls = [c for c in calls if c[:2] == ["git", "commit"]]
    assert commit_calls and commit_calls[0][-1] == "wip(S1): step-1"

    journal = json.loads((plan_dir / "ck.S1.journal.json").read_text())
    assert len(journal) == 1
    assert journal[0]["step"] == "step-1"
    assert journal[0]["summary"] == "Implemented the parser"
    assert journal[0]["next_hint"] == "write tests for edge cases"
    assert journal[0]["commit"] == "abc123"
    assert "ts" in journal[0]


def test_checkpoint_appends_multiple_entries_in_order(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck2", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    shas = iter(["sha-1", "sha-2"])

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = (next(shas) + "\n") if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    p.checkpoint("ck2", "S1", "step-1", "first")
    p.checkpoint("ck2", "S1", "step-2", "second")

    journal = json.loads((plan_dir / "ck2.S1.journal.json").read_text())
    assert [e["step"] for e in journal] == ["step-1", "step-2"]
    assert [e["commit"] for e in journal] == ["sha-1", "sha-2"]


def test_checkpoint_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "ck3", {})
    result = p.checkpoint("ck3", "NOPE", "step-1", "summary")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_checkpoint_nothing_to_commit_still_records_journal(plan_dir, tmp_path, monkeypatch):
    """If the agent already committed its own work (e.g. via Bash), git commit
    finds nothing staged. The checkpoint must still succeed and record the
    current HEAD sha rather than failing the whole call."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck4", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stdout = "nothing to commit, working tree clean\n"
        elif cmd[:2] == ["git", "rev-parse"]:
            Result.stdout = "existing-sha\n"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ck4", "S1", "step-1", "no new changes")
    assert result["ok"] is True
    assert result["commit"] == "existing-sha"


def test_checkpoint_nothing_to_commit_due_to_excluded_agent_log_still_records_journal(
    plan_dir, tmp_path, monkeypatch,
):
    """When the only untracked file is the excluded agent.log, git's "clean"
    message is "nothing added to commit but untracked files present" rather
    than "nothing to commit, working tree clean" - this must also count as
    a successful no-op, not an error."""
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck5", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stdout = (
                "On branch agent/x\n\nUntracked files:\n"
                '  (use "git add <file>..." to include in what will be committed)\n'
                "\tagent.log\n\n"
                "nothing added to commit but untracked files present "
                '(use "git add" to track)\n'
            )
        elif cmd[:2] == ["git", "rev-parse"]:
            Result.stdout = "existing-sha\n"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.checkpoint("ck5", "S1", "step-1", "no new changes")
    assert result["ok"] is True
    assert result["commit"] == "existing-sha"


def test_checkpoint_raises_on_real_commit_failure(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "ck5", {
        "S1": {"summary": "thing", "status": "in_progress", "worktree": worktree},
    })

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[:2] == ["git", "commit"]:
            Result.returncode = 1
            Result.stderr = "fatal: unable to write new index file"
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        p.checkpoint("ck5", "S1", "step-1", "summary")


def test_check_story_status_skips_test_run_for_interrupted_story(plan_dir):
    """An interrupted story is incomplete by definition — running its test
    suite would just record a spurious failure instead of staying resumable."""
    _write_manifest(plan_dir, "intr", {
        "S1": {"summary": "thing", "status": "interrupted", "pid": 999,
               "worktree": str(plan_dir / "wt"), "last_commit": "abc123"},
    })
    result = p.check_story_status("intr", "S1")
    assert result == {"status": "interrupted", "pid": 999}


# ---------- Step-cap exit routing (regression guard for PR #49) ----------
#
# When the headless agent hits its step cap it prints a terminal marker on
# its last log line, exits with code 2, and has already WIP-committed. The
# bug fixed by this block: check_story_status used to ignore the marker and
# fall straight through to running the test suite against the WIP commit,
# marking the story `tests_passed` and making the incomplete work merge-
# eligible (which is how PR #49 / commit 90a3cf1 landed in master). These
# tests pin the new routing: marker -> interrupted, no test run, journal
# entry written so dispatch_story can resume.

_STEP_CAP_MARKER_LOCAL = "[ended without done — step cap reached]"
_STEP_CAP_MARKER_ORACLE = "[ended without oracle green — step cap reached]"


def _make_fake_git_run(head_sha="deadbeef"):
    """Return a subprocess.run stub that mimics _commit_wip's git usage:
    `git add -A` (ok), `git reset -q -- agent.log` (ok), `git commit`
    (ok, no-op), `git rev-parse HEAD` (returns head_sha)."""
    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = f"{head_sha}\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    return _fake_run


def test_check_story_status_routes_step_cap_to_interrupted(
    plan_dir, tmp_path, monkeypatch,
):
    """Regression guard for PR #49: when the agent's last log line is the
    step-cap marker, the story must be marked interrupted (NOT tests_passed),
    no test suite is run, and a journal entry is appended so a subsequent
    dispatch_story call can resume."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "Working on it...\n"
        "[step 12] bash: pytest -q\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    # The test suite MUST NOT be invoked. detect_test_command is the gate
    # in front of subprocess.run for the test runner; if it gets called the
    # routing is broken and we'd silently re-introduce PR #49.
    def _fail_detect(*a, **k):
        raise AssertionError("detect_test_command must not run on a step-cap exit")
    monkeypatch.setattr(p, "detect_test_command", _fail_detect)
    # _commit_wip WILL be called (mirror interrupt_story); stub git so the
    # checkpoint path returns a clean sha without touching real git.
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap1", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "step_cap_reached"
    manifest = _read_manifest(plan_dir, "cap1")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    # Journal entry must exist so the resume path has context.
    journal = p._read_journal("cap1", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_step_cap_adds_cleanup_guidance_even_when_diagnosis_fails(
    plan_dir, tmp_path, monkeypatch,
):
    """Worktree-hygiene guidance must be applied UNCONDITIONALLY on every
    step-cap resume, independent of whether the diagnosis role succeeds -
    it is wired as a separate, unconditional call, not folded into
    _rebrief_step_cap_struggle's fail-open diagnosis path. Simulate the
    diagnosis role failing open (returns None, per diagnose_failure's
    documented contract) and confirm the cleanup guidance still lands in
    agent_instructions."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "Working on it...\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4243, "worktree": str(worktree),
               "agent_instructions": "GOAL: build the thing."},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                         lambda *a, **k: (_ for _ in ()).throw(
                             AssertionError("must not run on a step-cap exit")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    # Force the diagnosis role to fail open, exactly as diagnose_failure
    # does on an unconfigured/erroring role.
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)

    p.check_story_status("cap2", "S1")

    manifest = _read_manifest(plan_dir, "cap2")
    instructions = manifest["stories"]["S1"]["agent_instructions"]
    from pipeline import rebrief
    assert rebrief.CLEANUP_HEADER in instructions
    assert "GOAL: build the thing." in instructions
    # No diagnosis block, since diagnose_failure returned None.
    assert rebrief.DIAGNOSIS_HEADER not in instructions


def test_check_story_status_routes_infra_failure_to_interrupted_without_burning_rework(
    plan_dir, tmp_path, monkeypatch,
):
    """Found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD): a dispatch
    that died on an Ollama 500 (or timeout) after chat()'s own retries and
    the 5xx trim-retry are exhausted got treated exactly like a real review
    cycle - the test suite ran against its incomplete WIP and, worse, the
    infra death counted against rework_attempts, parking a story partly on
    infrastructure flakiness the model had no way to avoid. The last line
    must route to interrupted (no test run, dispatch-eligible for a clean
    resume) with rework_attempts UNCHANGED - distinct from the STEP_CAP_MARKERS
    routing, which shares the interrupted/no-test-run behavior but is a
    capability signal, not an infra one, so it's allowed to feed the
    model-fallback-switching logic that this path must NOT trigger."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[boot] pid=123 model=gpt-oss:20b endpoint=http://localhost:11434 provider=ollama steps=60 timeout=5400.0s\n"
        "[step 17] LLM call failed: Server error '500 Internal Server Error' for url 'http://localhost:11434/api/chat'\n"
    )
    _write_manifest(plan_dir, "infra1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree), "rework_attempts": 1},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fail_detect(*a, **k):
        raise AssertionError("detect_test_command must not run on an infra-failure exit")
    monkeypatch.setattr(p, "detect_test_command", _fail_detect)
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("infra1", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "infra_failure"
    manifest = _read_manifest(plan_dir, "infra1")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["rework_attempts"] == 1, (
        f"an infra death must not burn a rework attempt, got {story['rework_attempts']!r}"
    )
    journal = p._read_journal("infra1", "S1")
    assert any(e.get("step") == "infra_failure" for e in journal), journal


def test_check_story_status_infra_failure_does_not_trigger_model_fallback(
    plan_dir, tmp_path, monkeypatch,
):
    """An infra death is not evidence the MODEL is struggling - it must not
    feed the STEP_CAP_MARKERS branch's consecutive-failure model-fallback
    switch, even when the plan has opted in to local_model_fallback."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "dispatched_model": "gpt-oss:20b", "step_cap_streak": 2,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "infra2.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "devstral:24b"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra2", "S1")

    manifest = _read_manifest(plan_dir, "infra2")
    story = manifest["stories"]["S1"]
    assert story["model"] == "gpt-oss:20b", "infra death must not switch the model"


# ---------- infra-failure streak: visibility + bound on a persistent
# condition (2026-07-29) ----------
# Unlike STEP_CAP_MARKERS, the infra-failure branch had no streak counter, no
# threshold, no fallback, and no _notify_user - a persistent infra condition
# (a wedged Ollama server, a model too large for available memory) looped
# silently forever: dispatch, die, interrupted, redispatch, die again, with
# nothing to show and no notification. These mirror the step-cap streak
# tests above but use SEPARATE fields (infra_failure_streak /
# infra_failure_streak_model) so an infra death still never feeds the
# step-cap model-switch logic (see the does_not_trigger_model_fallback test
# above, which stays valid unmodified: its streak of 1 is below threshold).

def test_check_story_status_infra_failure_streak_notifies_on_first_occurrence(
    plan_dir, tmp_path, monkeypatch,
):
    """A single infra death is worth surfacing immediately - unlike a
    step-cap hit (routine for a local model), a transport failure after
    chat()'s own retries AND the 5xx trim-retry are exhausted is unusual
    enough to be worth a notification on the very first occurrence, not just
    after a streak."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b"},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra3", "S1")

    story = _read_manifest(plan_dir, "infra3")["stories"]["S1"]
    assert story["infra_failure_streak"] == 1
    assert story["infra_failure_streak_model"] == "gpt-oss:20b"
    notif = (plan_dir / "infra3.notifications.log").read_text()
    assert "infrastructure failure" in notif
    assert "gpt-oss:20b" in notif


def test_check_story_status_infra_failure_streak_switches_model_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra4", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
               "infra_failure_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "infra4.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("infra4", "S1")

    story = _read_manifest(plan_dir, "infra4")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"  # never claude
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    notif = (plan_dir / "infra4.notifications.log").read_text()
    assert "switching to fallback model glm-5.2:cloud" in notif


def test_check_story_status_infra_failure_streak_escalates_to_claude_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] LLM call failed after trim-retry: Server error '500'\n"
    )
    _write_manifest(plan_dir, "infra5", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "infra_failure_streak": p.INFRA_FAILURE_FALLBACK_THRESHOLD - 1,
               "infra_failure_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run tests")))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("infra5", "S1")

    assert result == {"status": "todo", "reason": "infra_failure_escalated_to_claude", "pid": 4242}
    story = _read_manifest(plan_dir, "infra5")["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    notif = (plan_dir / "infra5.notifications.log").read_text()
    assert "Claude" in notif
    assert "infrastructure failure" in notif


def test_escalate_to_claude_pops_infra_failure_streak_fields(
    plan_dir, tmp_path, monkeypatch,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escih.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "infra_failure_streak": 3, "infra_failure_streak_model": "gpt-oss:20b",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p._escalate_to_claude(manifest, "escih", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story
    assert story["backend"] == "claude"


# ---------- Escalation retarget (PIPELINE_ESCALATION_BACKEND/MODEL) ----------
# While Claude usage is capped, an operator can retarget escalation away from
# Claude so an escalated story re-dispatches/re-reviews on a non-Claude
# provider instead of failing against an unavailable Claude. The default
# (env unset) must preserve the original claude flip exactly, so the many
# existing escalation tests - none of which set these env vars - stay green.

def test_escalation_target_defaults_to_claude_with_no_model(monkeypatch):
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    assert p._escalation_target() == ("claude", None)


def test_escalation_target_env_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")
    assert p._escalation_target() == ("ollama", "deepseek-v4-flash:cloud")


def test_escalation_target_blank_model_yields_none(monkeypatch):
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "  ")
    assert p._escalation_target() == ("ollama", None)


def test_escalate_to_claude_retargets_to_env_backend_and_model(
    plan_dir, tmp_path, monkeypatch,
):
    """PIPELINE_ESCALATION_BACKEND/MODEL retarget the dispatch-failure
    escalation: the story flips to the configured backend (not Claude) and its
    model is set to the configured model so the next dispatch runs on it."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escesc.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "model": "gemma4:26b-a4b-it-qat",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")

    p._escalate_to_claude(manifest, "escesc", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert story["backend"] == "ollama"
    assert story["model"] == "deepseek-v4-flash:cloud"
    assert story["escalated"] is True
    assert story["status"] == "todo"


def test_escalate_to_claude_default_leaves_model_untouched(
    plan_dir, tmp_path, monkeypatch,
):
    """Default escalation target (claude, no model) must NOT overwrite or clear
    the story's existing model field - preserving the original behavior where
    Claude dispatch resolves its own model and the escalation only flips the
    backend."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escdef.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "model": "gemma4:26b-a4b-it-qat",
                   "dispatch_attempts": 1},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)

    p._escalate_to_claude(manifest, "escdef", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story.get("model") == "gemma4:26b-a4b-it-qat"


def test_escalate_review_to_claude_retargets_to_env_backend_and_model(monkeypatch):
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "deepseek-v4-flash:cloud")
    story = {"backend": "local", "model": "gemma4:26b-a4b-it-qat",
             "rework_attempts": 3, "review_inconclusive_count": 2}

    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    assert story["backend"] == "ollama"
    assert story["model"] == "deepseek-v4-flash:cloud"
    assert story["escalated"] is True
    assert "rework_attempts" not in story
    assert "review_inconclusive_count" not in story


def test_escalate_review_to_claude_default_preserves_claude_no_model(monkeypatch):
    monkeypatch.setattr("pipeline.escalation._notify_user", lambda *a, **k: None)
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    story = {"backend": "local", "model": "gemma4:26b-a4b-it-qat",
             "rework_attempts": 3}

    p._escalate_review_to_claude(story, "S1", "escplan", "rework budget exhausted")

    assert story["backend"] == "claude"
    assert story["escalated"] is True
    # default target has no model -> existing model field is left untouched
    assert story.get("model") == "gemma4:26b-a4b-it-qat"


def test_check_story_status_routes_oracle_step_cap_to_interrupted(
    plan_dir, tmp_path, monkeypatch,
):
    """The oracle agent uses a different marker. It must be classified the
    same way: interrupted, not tests_passed, no test run."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        f"{_STEP_CAP_MARKER_ORACLE}\n"
    )
    _write_manifest(plan_dir, "cap2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on an oracle step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="cafe0000"))

    result = p.check_story_status("cap2", "S1")

    assert result["status"] == "interrupted"
    assert result["reason"] == "step_cap_reached"
    manifest = _read_manifest(plan_dir, "cap2")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    assert manifest["stories"]["S1"]["last_commit"] == "cafe0000"
    journal = p._read_journal("cap2", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_step_cap_streak_ignored_without_fallback_configured(
    plan_dir, tmp_path, monkeypatch,
):
    """A plan with no manifest["local_model_fallback"] (the default for every
    plan except the ones that opt in) must not track or act on a step-cap
    streak at all - existing behavior for the vast majority of plans is
    unchanged. Pinned to non-auto dispatch: under PIPELINE_BACKEND_DISPATCH=
    auto a no-fallback plan now escalates to Claude instead (see the
    escalates_to_claude_at_threshold test below), so this test's "no streak
    tracking at all" guarantee only holds outside auto mode - pin it
    explicitly rather than relying on the ambient shell env."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap3", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatched_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap3", "S1")

    story = _read_manifest(plan_dir, "cap3")["stories"]["S1"]
    assert "model" not in story  # never set - no fallback configured for this plan
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story


def test_check_story_status_step_cap_streak_increments_below_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """A plan opted into local_model_fallback tracks consecutive step-cap
    interrupts on the same model, but does not switch until the threshold
    (default 3) is reached."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap4", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "dispatched_model": "gpt-oss:20b", "step_cap_streak": 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap4.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap4", "S1")

    story = _read_manifest(plan_dir, "cap4")["stories"]["S1"]
    assert story["model"] == "gpt-oss:20b"  # not switched yet
    assert story["step_cap_streak"] == 2
    assert story["step_cap_streak_model"] == "gpt-oss:20b"


def test_check_story_status_step_cap_streak_switches_model_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """Once the step-cap streak on the same model reaches
    STEP_CAP_FALLBACK_THRESHOLD, the story's model switches to the plan's
    fallback model for the next resume - backend is untouched (stays local,
    never Claude), and the streak counters reset."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap5", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap5.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap5", "S1")

    story = _read_manifest(plan_dir, "cap5")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"  # never claude
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    notif = (plan_dir / "cap5.notifications.log").read_text()
    assert "switching to fallback model glm-5.2:cloud" in notif


def test_check_story_status_step_cap_streak_noop_once_already_on_fallback_model(
    plan_dir, tmp_path, monkeypatch,
):
    """A story already running on the plan's fallback model that keeps
    hitting the step cap must not restart the streak counters or fire
    another switch-model notification - there is no fallback past the
    fallback, so the guard (current model == fallback model) must hold."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap6", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "glm-5.2:cloud",
               "backend": "local", "dispatched_model": "glm-5.2:cloud"},
    })
    manifest_path = plan_dir / "cap6.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap6", "S1")

    story = _read_manifest(plan_dir, "cap6")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    notif_path = plan_dir / "cap6.notifications.log"
    assert not notif_path.exists() or "switching to fallback model" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_ignores_claude_backend_story(
    plan_dir, tmp_path, monkeypatch,
):
    """The step-cap streak fallback only ever applies to a local-backend
    story. A story dispatched on backend="claude" must never have its model
    switched by this logic, even if a plan opts into local_model_fallback and
    the streak threshold is reached (defense in depth alongside the local
    agent scripts being the only source of STEP_CAP_MARKERS)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap7", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "sonnet",
               "backend": "claude", "dispatched_model": "sonnet",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "sonnet"},
    })
    manifest_path = plan_dir / "cap7.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p.check_story_status("cap7", "S1")

    story = _read_manifest(plan_dir, "cap7")["stories"]["S1"]
    assert story["model"] == "sonnet"  # never switched
    assert story["backend"] == "claude"
    notif_path = plan_dir / "cap7.notifications.log"
    assert not notif_path.exists() or "switching to fallback model" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_escalates_to_claude_at_threshold(
    plan_dir, tmp_path, monkeypatch,
):
    """Under PIPELINE_BACKEND_DISPATCH=auto, a plan with NO
    local_model_fallback configured must not spin forever on a struggling
    local model: once the step-cap streak reaches STEP_CAP_FALLBACK_THRESHOLD
    the story escalates to Claude via the same clean-slate teardown
    _escalate_to_claude already performs for test-failure escalation
    (worktree/branch removed, journal cleared, backend flips to claude,
    status reset to todo) - and check_story_status returns early with the
    step-cap-specific reason instead of reporting stale 'interrupted'."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap8", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap8", "S1")

    assert result == {"status": "todo", "reason": "step_cap_escalated_to_claude", "pid": 4242}
    story = _read_manifest(plan_dir, "cap8")["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls)
    journal_path = plan_dir / "cap8.S1.journal.json"
    assert not journal_path.exists()
    notif = (plan_dir / "cap8.notifications.log").read_text()
    assert "Claude" in notif
    assert "step cap" in notif.lower()


def test_check_story_status_step_cap_streak_below_threshold_no_claude_escalation(
    plan_dir, tmp_path, monkeypatch,
):
    """Same setup as the threshold-crossing test, but the streak has not yet
    reached STEP_CAP_FALLBACK_THRESHOLD: the story must stay 'interrupted',
    backend must be untouched, the streak fields must be incremented (never
    reset), and no teardown or notification must fire."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap9", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": 1, "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap9", "S1")

    assert result["status"] == "interrupted"
    story = _read_manifest(plan_dir, "cap9")["stories"]["S1"]
    assert story["backend"] == "local"
    assert story["step_cap_streak"] == 2
    assert story["step_cap_streak_model"] == "gpt-oss:20b"
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)
    notif_path = plan_dir / "cap9.notifications.log"
    assert not notif_path.exists() or "escalating to Claude" not in notif_path.read_text()


def test_check_story_status_step_cap_streak_local_fallback_takes_priority_over_claude(
    plan_dir, tmp_path, monkeypatch,
):
    """Regression guard: local_model_fallback and Claude escalation are
    mutually exclusive with no chaining. When a plan has opted into
    local_model_fallback, crossing the threshold under
    PIPELINE_BACKEND_DISPATCH=auto must still route through the existing
    local-fallback-model switch, never through Claude escalation."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap10", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    manifest_path = plan_dir / "cap10.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap10", "S1")

    story = _read_manifest(plan_dir, "cap10")["stories"]["S1"]
    assert story["model"] == "glm-5.2:cloud"
    assert story["backend"] == "local"
    assert "escalated" not in story
    assert result["status"] == "interrupted"


def test_check_story_status_step_cap_streak_local_fallback_never_escalates_to_claude(
    plan_dir, tmp_path, monkeypatch,
):
    """Sibling to test_check_story_status_step_cap_streak_noop_once_already_on_
    fallback_model: even when the streak on the fallback model itself is
    already several multiples past STEP_CAP_FALLBACK_THRESHOLD (i.e. the
    fallback model keeps step-capping too) and PIPELINE_BACKEND_DISPATCH=
    auto, a plan with local_model_fallback configured must never escalate to
    Claude - no chaining from local fallback to Claude."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap11", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "glm-5.2:cloud",
               "backend": "local", "dispatched_model": "glm-5.2:cloud",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD * 5,
               "step_cap_streak_model": "glm-5.2:cloud"},
    })
    manifest_path = plan_dir / "cap11.manifest.json"
    m = json.loads(manifest_path.read_text())
    m["local_model_fallback"] = "glm-5.2:cloud"
    manifest_path.write_text(json.dumps(m))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap11", "S1")

    story = _read_manifest(plan_dir, "cap11")["stories"]["S1"]
    assert story["backend"] == "local"
    assert story.get("model") == "glm-5.2:cloud"
    assert "escalated" not in story
    assert result["status"] == "interrupted"
    assert story["step_cap_streak"] == p.STEP_CAP_FALLBACK_THRESHOLD * 5
    assert story["step_cap_streak_model"] == "glm-5.2:cloud"


@pytest.mark.parametrize("dispatch_env", ["local", "claude", None])
def test_check_story_status_step_cap_streak_noop_without_auto_dispatch(
    plan_dir, tmp_path, monkeypatch, dispatch_env,
):
    """No local_model_fallback configured AND PIPELINE_BACKEND_DISPATCH is
    not 'auto' (explicit 'local', explicit 'claude', or unset/default): the
    new Claude-escalation branch must never fire, and today's byte-for-byte
    behavior is preserved - the streak fields are not even created."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap12", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "backend": "local",
               "dispatched_model": "gpt-oss:20b"},
    })
    if dispatch_env is None:
        monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", dispatch_env)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap12", "S1")

    story = _read_manifest(plan_dir, "cap12")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert story["backend"] == "local"


def test_check_story_status_step_cap_streak_does_not_reescalate_already_escalated_story(
    plan_dir, tmp_path, monkeypatch,
):
    """Mirrors test_advance_pipeline_does_not_escalate_already_escalated's
    invariant for the step-cap streak path: a story with escalated=True must
    never be escalated a second time, even past the threshold. Uses a
    backend='local', escalated=True fixture explicitly (not inferred from
    backend=='claude')."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap13", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "gpt-oss:20b",
               "backend": "local", "dispatched_model": "gpt-oss:20b",
               "escalated": True,
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "gpt-oss:20b"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "deadbeef\n" if cmd[:3] == ["git", "rev-parse", "HEAD"] else ""
            stderr = ""
        return Result()
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("cap13", "S1")

    story = _read_manifest(plan_dir, "cap13")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert story["backend"] == "local"
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls)
    assert not any(c[:3] == ["git", "branch", "-D"] for c in calls)


def test_check_story_status_step_cap_streak_ignores_claude_backend_without_fallback_configured(
    plan_dir, tmp_path, monkeypatch,
):
    """Extends test_check_story_status_step_cap_streak_ignores_claude_backend_
    story to the no-fallback-configured case: a story already on backend=
    'claude' with streak fields present must never enter the new
    Claude-escalation branch, even under PIPELINE_BACKEND_DISPATCH=auto with
    no local_model_fallback configured (defense in depth - STEP_CAP_MARKERS
    are only ever printed by local agent scripts)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(f"{_STEP_CAP_MARKER_LOCAL}\n")
    _write_manifest(plan_dir, "cap14", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "model": "sonnet",
               "backend": "claude", "dispatched_model": "sonnet",
               "step_cap_streak": p.STEP_CAP_FALLBACK_THRESHOLD - 1,
               "step_cap_streak_model": "sonnet"},
    })
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    result = p.check_story_status("cap14", "S1")

    story = _read_manifest(plan_dir, "cap14")["stories"]["S1"]
    assert result["status"] == "interrupted"
    assert story["backend"] == "claude"
    assert story["step_cap_streak"] == p.STEP_CAP_FALLBACK_THRESHOLD - 1
    assert story["step_cap_streak_model"] == "sonnet"


def test_escalate_to_claude_pops_step_cap_streak_fields(
    plan_dir, tmp_path, monkeypatch,
):
    """_escalate_to_claude is now also invoked from the step-cap streak path
    (see check_story_status), in addition to its original test-failure
    caller. Its existing pop-key teardown must additionally clear
    step_cap_streak / step_cap_streak_model so stale local-streak state never
    lingers on a story that has moved to the claude backend."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest_path = plan_dir / "escg.manifest.json"
    manifest = {
        "epics": {},
        "stories": {
            "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
                   "worktree": str(worktree), "backend": "local",
                   "step_cap_streak": 3, "step_cap_streak_model": "gpt-oss:20b",
                   "dispatch_attempts": 1, "dispatch_error": "boom"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))

    p._escalate_to_claude(manifest, "escg", "S1", manifest_path)

    story = manifest["stories"]["S1"]
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert story["status"] == "todo"


def test_check_story_status_normal_completion_still_routes_to_tests_passed(
    plan_dir, tmp_path, monkeypatch,
):
    """Negative case: a normal agent run whose last log line is NOT the
    step-cap marker must continue to route through the test suite and land
    on tests_passed. This is the regression guard against over-broad marker
    detection (substring-in-whole-file would have caught this case too, but
    we want to be explicit)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n")
    _write_manifest(plan_dir, "normal1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("normal1", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "normal1")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


def test_check_story_status_successful_dispatch_clears_both_streaks(
    plan_dir, tmp_path, monkeypatch,
):
    """Both streak counters describe CONSECUTIVE failures, but neither was
    ever cleared on a successful dispatch (only dispatch_attempts was) - so
    two infra deaths early plus one much later escalated as "3 consecutive"
    even with real progress in between. A dispatch that produced output and
    ran its tests breaks any streak, so both must reset here."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("Done.\nAll tests pass.\n")
    _write_manifest(plan_dir, "streakclear", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree),
               "step_cap_streak": 2, "step_cap_streak_model": "gpt-oss:20b",
               "infra_failure_streak": 2, "infra_failure_streak_model": "gpt-oss:20b",
               "dispatch_attempts": 1},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "all green"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    p.check_story_status("streakclear", "S1")

    story = _read_manifest(plan_dir, "streakclear")["stories"]["S1"]
    assert "dispatch_attempts" not in story
    assert "step_cap_streak" not in story
    assert "step_cap_streak_model" not in story
    assert "infra_failure_streak" not in story
    assert "infra_failure_streak_model" not in story


def test_check_story_status_step_cap_clean_worktree_no_crash(
    plan_dir, tmp_path, monkeypatch,
):
    """Boundary: marker present but the worktree is already clean (the
    agent hit the cap right after its own WIP commit, so there's nothing
    extra to checkpoint). The routing must still fire without crashing,
    using HEAD as the checkpoint sha."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "some prior output\n"
        f"{_STEP_CAP_MARKER_LOCAL}\n"
    )
    _write_manifest(plan_dir, "cap3", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("detect_test_command must not run on a step-cap exit")
    ))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="clean000"))

    result = p.check_story_status("cap3", "S1")
    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "cap3")
    assert manifest["stories"]["S1"]["status"] == "interrupted"
    assert manifest["stories"]["S1"]["last_commit"] == "clean000"
    journal = p._read_journal("cap3", "S1")
    assert any(e.get("step") == "step_cap_reached" for e in journal), journal


def test_check_story_status_ignores_old_marker_then_normal_done(
    plan_dir, tmp_path, monkeypatch,
):
    """Boundary: on a resume the log is appended to, so a prior step-cap
    marker from a previous tick may appear earlier in the file. The
    classification must look ONLY at the last non-empty line, so the
    later successful done marker wins and the story routes to tests_passed
    like any other normal run."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        f"resume tick 1...\n{_STEP_CAP_MARKER_LOCAL}\n"
        "resume tick 2...\nDone.\n"
    )
    _write_manifest(plan_dir, "resume1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "ok"
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("resume1", "S1")
    # Old marker must not poison the routing — last line is "Done.", which
    # is a normal completion.
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "resume1")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def test_dispatch_story_fresh_creates_worktree_and_dispatches(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "ds", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds", "S1")

    assert result["ok"] is True
    assert result["pid"] == 1234
    assert result["resumed"] is False
    assert ["git", "worktree", "add", "-b", "agent/s1", str(worktree_root / "S1"),
            "origin/main"] in run_calls
    assert ["git", "fetch", "origin", "main"] in run_calls
    assert not any(c[:2] == ["git", "pull"] for c in run_calls)

    manifest = _read_manifest(plan_dir, "ds")
    story = manifest["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 1234
    assert story["worktree"] == str(worktree_root / "S1")


def test_dispatch_story_passes_rework_full_suite_when_ci_rework_set(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """L1 threading: a story carrying `ci_rework` (set by the merge-CI rework
    router) must reach the agent subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1
    so the harness raises the done-bar to full-suite-green on the redispatch."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    # The story below carries an `acceptance` block, so dispatch_story's
    # pre-dispatch oracle gate also calls subprocess.run (unlike the
    # fire-and-forget git calls elsewhere in dispatch_story, it reads
    # .returncode/.stdout) - give it a real CompletedProcess so the gate
    # classifies this as an ordinary not-yet-implemented failure rather than
    # a broken oracle.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4321),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "cirs", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "ci_rework": True,
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("cirs", "S1")
    assert result["ok"] is True
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_story_omits_rework_full_suite_without_ci_rework(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard: a fresh dispatch (no ci_rework) must NOT set
    LOCAL_AGENT_REWORK_FULL_SUITE, or the full-suite done-bar would silently
    apply to cold-start dispatches."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    # See the sibling test above: this story also carries an `acceptance`
    # block, so the pre-dispatch oracle gate needs a real CompletedProcess
    # from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4322),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "cirsnone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [],
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("cirsnone", "S1")
    assert result["ok"] is True
    assert "LOCAL_AGENT_REWORK_FULL_SUITE" not in captured["env"]


def test_dispatch_story_passes_rework_full_suite_when_review_feedback_set(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """L1 gap: a story carrying reviewer `review_feedback` (a REQUEST_CHANGES
    rework redispatch, not a CI-fail rework) must ALSO reach the agent
    subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1. Without this, the
    reviewer-rework path lets the agent call `done` on a dirty/broken tree
    the reviewer never re-checked in full - the acceptance oracle stays
    green even when the agent's own edit broke the rest of the suite
    (observed live 2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD cycle 3: a
    botched replace_lines orphaned a function definition, the agent called
    done with 79 tests failing, and nothing rejected it)."""
    captured: dict = {}
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakeProc(4323),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    _write_manifest(plan_dir, "revfb", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "dependencies": [],
               "review_feedback": "REQUEST_CHANGES: fix the docstring placement.",
               "acceptance": [{"path": "tests/test_a.py", "source": "def test_a(): pass"}]},
    })

    result = p.dispatch_story("revfb", "S1")
    assert result["ok"] is True
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Mode 17: review.log (written by the local review loop) gets committed
    by a rework cycle's auto-WIP-commit if it isn't excluded, so the next
    review cycle's append makes it a modified TRACKED file - the pre-merge
    rebase then refuses ("You have unstaged changes"), failing an already
    -APPROVED, ground-truth-correct story. Excluding review.log (and
    agent.log, for the same reason) via .git/info/exclude at worktree
    creation means `git add -A` can never track them in the first place.
    Uses a REAL git repo (not mocked subprocess) so the actual exclude file
    content is verified end-to-end, not just that some git command ran."""
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=real_repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                    cwd=real_repo, check=True)
    bare_origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare_origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_origin)], cwd=real_repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=real_repo, check=True)

    _write_manifest(plan_dir, "excl", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "excl.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    # backend.subprocess IS the real, global subprocess module (a singleton
    # import, not a copy) - patching its Popen would also break the REAL
    # git commands this test needs (git worktree add / pull / add / diff).
    # Discriminate: only fake the actual dispatch invocation (argv[0] ==
    # "claude"), delegate everything else to the real Popen.
    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(999)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    result = p.dispatch_story("excl", "S1")
    assert result["ok"] is True

    exclude_content = (real_repo / ".git" / "info" / "exclude").read_text()
    assert "review.log" in exclude_content
    assert "agent.log" in exclude_content

    # The exclusion must actually work, not just be present as text: a
    # rework-style `git add -A` inside the worktree must not stage either
    # file, even when both exist with real content.
    worktree_path = worktree_root / "S1"
    (worktree_path / "review.log").write_text("review cycle 1\n")
    (worktree_path / "agent.log").write_text("[step 0] bash: ls\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=worktree_path, capture_output=True, text=True, check=True)
    assert "review.log" not in staged.stdout
    assert "agent.log" not in staged.stdout


def test_agent_plan_src_hash_is_excluded_from_worktree_tracking(tmp_path):
    """Regression guard for the .agent_plan_src_hash leak (2026-08-13): the
    checklist-reuse guard's companion hash file is an untracked runtime
    artifact (like .agent_plan.md) that must be in _WORKTREE_LOG_EXCLUDES so a
    rework WIP-commit's `git add -A` never tracks it - a tracked copy caused an
    add/add rebase conflict that terminal-failed P3-6's otherwise-green merge
    gate. Mirrors test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking's
    staging assertion, exercised directly against the exclude helper."""
    from pipeline.paths import (
        _WORKTREE_LOG_EXCLUDES,
        _exclude_worktree_logs_from_tracking,
    )

    # The artifact must be in the exclude list (the one-line fix).
    assert ".agent_plan_src_hash" in _WORKTREE_LOG_EXCLUDES

    # The exclude helper must write it to the repo's .git/info/exclude (the
    # shared, per-repo file that governs every worktree of the repo).
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=repo, check=True)
    _exclude_worktree_logs_from_tracking(repo)
    exclude_content = (repo / ".git" / "info" / "exclude").read_text()
    assert ".agent_plan_src_hash" in exclude_content

    # The exclusion must actually work: a `git add -A` in the working tree must
    # not stage the artifact even when it exists with real content.
    (repo / ".agent_plan_src_hash").write_text("deadbeef\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True, check=True)
    assert ".agent_plan_src_hash" not in staged.stdout


def test_dispatch_story_fresh_seeds_checkpoint_instruction_with_plan_name(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    _write_manifest(plan_dir, "ds4", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    popen_calls = []

    def _fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeProc(9999)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("ds4", "S1")

    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "checkpoint" in prompt.lower()
    assert "ds4" in prompt
    assert "S1" in prompt


def test_dispatch_story_resume_reuses_worktree_and_seeds_journal(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "ds2", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "worktree": str(worktree_path),
               "last_commit": "sha-1"},
    })
    (plan_dir / "ds2.S1.journal.json").write_text(json.dumps([
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]))

    run_calls = []
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))

    def _fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeProc(5555)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds2", "S1")

    assert result["ok"] is True
    assert result["resumed"] is True
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)
    assert not any(c[:2] == ["git", "pull"] for c in run_calls)

    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "RESUMING" in prompt
    assert "Wrote the parser" in prompt
    assert "add validation" in prompt

    manifest = _read_manifest(plan_dir, "ds2")
    story = manifest["stories"]["S1"]
    assert story["status"] == "in_progress"
    assert story["pid"] == 5555
    assert story["worktree"] == str(worktree_path)


def test_dispatch_story_changes_requested_seeds_review_feedback(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    # Redispatching a changes_requested story must feed the reviewer's stored
    # feedback into the agent's prompt so it reworks the right thing.
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "dscr", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: popen_calls.append(cmd) or _FakeProc(5556))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dscr", "S1")

    assert result["resumed"] is True
    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "The SQL is injectable; parameterize it." in prompt
    assert _read_manifest(plan_dir, "dscr")["stories"]["S1"]["status"] == "in_progress"


def test_dispatch_story_resumes_when_worktree_exists_even_without_interrupted_status(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story can be re-dispatched manually after a kill that never reached
    interrupt_story. Detect the leftover worktree and resume rather than
    failing on `git worktree add` for a path that already exists."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "ds3", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "failed", "worktree": str(worktree_path)},
    })

    run_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(7777))
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("ds3", "S1")

    assert result["resumed"] is True
    assert not any(c[:3] == ["git", "worktree", "add"] for c in run_calls)


# ---------- Local-first routing ----------

def test_route_dispatch_backend_low_risk_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "low", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_medium_risk_above_low_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_medium_risk_within_medium_ceiling_goes_local(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "medium", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_high_risk_above_medium_ceiling_goes_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "medium")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "claude"


def test_route_dispatch_backend_high_ceiling_allows_high_risk_local(monkeypatch):
    """Explicitly setting the ceiling to 'high' lets even high-risk go local."""
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "high", "persona": "software-engineer"}) == "local"


def test_route_dispatch_backend_security_persona_always_claude(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    assert p._route_dispatch_backend({"risk": "low", "persona": "security-engineer"}) == "claude"


def test_route_dispatch_backend_missing_risk_defaults_low(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    assert p._route_dispatch_backend({"persona": "software-engineer"}) == "local"


def test_dispatch_story_auto_routes_low_risk_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a low-risk story to OllamaDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "low", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(11))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("auto1", "S1")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "auto1")
    assert manifest["stories"]["S1"]["backend"] == "local"
    # OllamaDriver uses venv python, not "claude". test_author (claude/sonnet
    # per model_registry.json) issues its own leading Popen call first, so
    # the executor's call is the last one.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_auto_routes_high_risk_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=auto sends a high-risk story to ClaudeCliDriver."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "low")
    _write_manifest(plan_dir, "auto2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build it.",
               "status": "todo", "risk": "high", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(22))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("auto2", "S1")

    manifest = _read_manifest(plan_dir, "auto2")
    assert manifest["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_persists_backend_to_manifest(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The resolved backend is written to story['backend'] so escalation sees it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(33))
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb1", "S1")

    assert _read_manifest(plan_dir, "pb1")["stories"]["S1"]["backend"] == "local"


def test_dispatch_story_honors_stored_backend_override(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """story['backend'] takes precedence over env, used by escalation to lock to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "pb2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "backend": "claude"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(44))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("pb2", "S1")

    # Even though env says local, the stored override wins → ClaudeCliDriver
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_security_persona_routes_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=local must not bypass the security-persona
    safety override: a security-engineer story still dispatches to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "security-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(55))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec1", "S1")

    assert _read_manifest(plan_dir, "sec1")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_non_security_persona_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Non-skip personas are unaffected by the security-persona override under
    explicit local dispatch - no regression from the new persona check."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "sec2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "software-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(56))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec2", "S1")

    assert _read_manifest(plan_dir, "sec2")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_local_missing_persona_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story with no persona field at all must not crash the override check
    and must fall through to the existing local resolution."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "sec3", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(57))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec3", "S1")

    assert _read_manifest(plan_dir, "sec3")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_local_security_persona_case_insensitive(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The persona override must use the same case-insensitive normalization
    as _route_dispatch_backend, so 'Security-Engineer' is still caught."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec4", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "Security-Engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(58))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec4", "S1")

    assert _read_manifest(plan_dir, "sec4")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_stored_backend_wins_over_security_persona(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An already-escalated story['backend'] (e.g. from _escalate_to_local_fallback_model)
    must not be re-routed by the persona check even if persona is security-engineer."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec5", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "persona": "security-engineer", "backend": "local"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(59))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec5", "S1")

    # story["backend"] was already explicitly "local" - the persona override
    # must not clobber it, even though persona is in _LOCAL_SKIP_PERSONAS.
    assert _read_manifest(plan_dir, "sec5")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_dispatch_story_explicit_claude_security_persona_stays_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=claude is already always Claude; the persona
    override must be a no-op here (no regression)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    (agents_dir / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: opus\n---\n\nSecurity body.\n'
    )
    _write_manifest(plan_dir, "sec6", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [], "persona": "security-engineer"},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(60))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("sec6", "S1")

    assert _read_manifest(plan_dir, "sec6")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


# ---------- Unwinnable-as-scoped safety override (Mode 40 retro #4) ----------
# A story whose agent_instructions describe a repo-wide, unscoped lint/fix
# sweep is structurally unwinnable for local dispatch: the done-bar demands
# every finding fixed, but a meaningful fraction of findings routinely land
# in test files (35/83 files on the live ruff baseline that motivated this),
# which the never-touch-tests steering forbids the local executor from
# editing. Detected and hard-routed to Claude, mirroring the existing
# security-persona override exactly.

def test_story_has_unwinnable_local_scope_detects_repo_wide_ruff_sweep():
    story = {"agent_instructions": "Run `.venv/bin/ruff check . --fix` from "
             "the repo root and fix every remaining finding."}
    assert p._story_has_unwinnable_local_scope(story) is True


def test_story_has_unwinnable_local_scope_detects_repo_wide_language():
    story = {"agent_instructions": "Fix every lint finding repo-wide."}
    assert p._story_has_unwinnable_local_scope(story) is True


def test_story_has_unwinnable_local_scope_false_for_scoped_lint_instructions():
    story = {"agent_instructions": "Run `ruff check pipeline/foo.py` and fix "
             "the two findings in that file."}
    assert p._story_has_unwinnable_local_scope(story) is False


def test_story_has_unwinnable_local_scope_false_for_missing_instructions():
    assert p._story_has_unwinnable_local_scope({}) is False


def test_route_dispatch_backend_unwinnable_scope_overrides_low_risk(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_RISK", "high")
    story = {"risk": "low", "persona": "software-engineer",
              "agent_instructions": "Run `ruff check .` repo-wide and fix "
              "every finding."}
    assert p._route_dispatch_backend(story) == "claude"


def test_dispatch_story_explicit_local_unwinnable_scope_routes_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_BACKEND_DISPATCH=local must not bypass the unwinnable-scope
    safety override: a repo-wide lint-sweep story still dispatches to Claude."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "scope1", {
        "S1": {"summary": "Adopt new lint ruleset",
               "agent_instructions": "Run `ruff check . --fix` from the repo "
               "root and manually fix every remaining finding.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(60))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("scope1", "S1")

    assert _read_manifest(plan_dir, "scope1")["stories"]["S1"]["backend"] == "claude"
    assert popen_calls[0][0] == "claude"


def test_dispatch_story_explicit_local_scoped_lint_stays_local(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A lint-flavored story scoped to specific files (not a repo-wide sweep)
    is unaffected by the new override - no regression on ordinary stories."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "scope2", {
        "S1": {"summary": "Fix two lint findings",
               "agent_instructions": "Run `ruff check pipeline/foo.py` and "
               "fix the reported findings in that file only.",
               "status": "todo", "dependencies": []},
    })
    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: (popen_calls.append(cmd), _FakeProc(61))[1])
    monkeypatch.setattr(pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("scope2", "S1")

    assert _read_manifest(plan_dir, "scope2")["stories"]["S1"]["backend"] == "local"
    # test_author (claude/sonnet) issues a leading Popen call first.
    assert popen_calls[-1][0] != "claude"


def test_advance_pipeline_escalates_local_failure_to_claude(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local agent failure triggers clean Claude escalation (not terminal failure)
    under auto dispatch, where a-posteriori escalation is the intended fallback."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc1", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9001,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        # The internal `ps -p <pid> -o stat=` lookup should return empty so
        # check_story_status doesn't mistakenly treat the dead process as
        # "running". The test command itself returns _FailResult.
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc1")

    manifest = _read_manifest(plan_dir, "esc1")
    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert "pid" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "esc1.notifications.log").read_text()
    assert "escalating to Claude" in notif


def test_advance_pipeline_does_not_escalate_claude_failure(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A Claude-run failure is terminal (not escalated again)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc2", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9002,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "claude", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    # Pretend the agent has exited: os.kill throws AND ps reports no such
    # process (empty stdout). check_story_status falls through both checks
    # and runs the test command (also mocked to fail). See esc1 for full
    # context.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc2")

    manifest = _read_manifest(plan_dir, "esc2")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_does_not_escalate_already_escalated(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story that already has escalated=True is not escalated a second time."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc3", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9003,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "escalated": True, "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    # The story's OWN pid (9003) must look dead (kill raises, ps returns
    # empty) for check_story_status to grade it. Other (zombie) pids also
    # raise; the production code's reap path turns this story's dead pid
    # back to "todo" — which is correct semantics for a real crashed agent.
    # See esc1 comment for the broader picture.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc3")

    manifest = _read_manifest(plan_dir, "esc3")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_escalates_local_failure_under_explicit_local_dispatch_with_auto_escalate_flag(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local agent failure escalates to Claude even when the dispatcher is
    explicitly pinned to ``local`` (PIPELINE_BACKEND_DISPATCH=local), as long
    as PIPELINE_AUTO_ESCALATE=1 turns the a-posteriori escalation gate on.

    This is the third escalation call site in advance_pipeline (the
    a-posteriori local-failure escalation). Before the fix it used a raw
    ``dispatch_mode == "auto"`` comparison, so under an explicit local backend
    escalation never fired and the story terminal-failed instead. After the fix
    it consults ``_auto_escalation_enabled()`` and honors the explicit opt-in.
    """
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc4", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9004,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc4")

    manifest = _read_manifest(plan_dir, "esc4")
    story = manifest["stories"]["S1"]
    assert story["backend"] == "claude"
    assert story["status"] == "todo"
    assert story["escalated"] is True
    assert "pid" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "esc4.notifications.log").read_text()
    assert "escalating to Claude" in notif


def test_advance_pipeline_does_not_escalate_local_failure_when_auto_escalate_explicitly_off(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When PIPELINE_AUTO_ESCALATE=0 (explicit opt-out), a failing local story
    under auto dispatch does NOT escalate to Claude - it terminal-fails.

    This confirms the explicit opt-out (already proven at the escalation.py
    unit level in tests/unit/test_acceptance_autonomy_escalate_flag.py) is also
    honored at this specific a-posteriori call site in advance_pipeline, which
    was never covered here before. Before the fix this call site ignored
    PIPELINE_AUTO_ESCALATE entirely (it only checked dispatch_mode=="auto"),
    so the opt-out would have been silently bypassed and escalation would have
    fired anyway.
    """
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esc5", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9005,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "0")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esc5")

    manifest = _read_manifest(plan_dir, "esc5")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "S1" in result.get("failed", [])
    assert story.get("backend") != "claude"


def test_advance_pipeline_aposteriori_escalation_uses_auto_escalation_gate():
    """The a-posteriori local-failure escalation call site in advance_pipeline
    must use the shared ``_auto_escalation_enabled()`` gate (not a raw
    ``dispatch_mode == "auto"`` env-var comparison), and the now-dead
    ``dispatch_mode`` local variable must be removed entirely from the file.

    These are mechanically-checkable requirements of the fix: the third call
    site is made consistent with the other two, and per project style the dead
    ``dispatch_mode`` definition is removed (not left unused/commented out).
    """
    import pipeline.server as srv

    source = Path(srv.__file__).read_text()

    # The a-posteriori escalation block's comment must reference the new gate,
    # not the old "auto dispatch only" wording.
    assert "_auto_escalation_enabled()" in source
    assert "A-posteriori escalation of a failed local run to Claude is gated by" in source
    # The old comment wording must be gone.
    assert (
        "A-posteriori escalation of a failed local run to Claude is a feature of"
        not in source
    )

    # The dead `dispatch_mode` local variable must be entirely gone - both its
    # definition and its single use at the escalation condition.
    assert "dispatch_mode" not in source


def test_advance_pipeline_give_up_failure_gets_distinguishing_notify(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story whose agent explicitly gave up (T6) gets a notify message
    pointing the human at "likely under-specified, needs clarification"
    rather than the generic "tests failed" - so a second identical
    dispatch/escalation attempt isn't the reflexive next step (2026-07-07
    web-client-epic retro §3.2: a missing API is a story-scoping bug, not a
    model-capability gap). Mirrors test_advance_pipeline_does_not_escalate_
    already_escalated's terminal-branch setup exactly, differing only in
    agent.log's content and the assertion on the notify message."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text(
        "[step 1] bash: grep -r PreKeyBundle .\n"
        "[step 9] DONE: I'm sorry, I can't complete this task.\n"
    )
    _write_manifest(plan_dir, "giveupnotify", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9003,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "escalated": True, "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.advance_pipeline("giveupnotify")

    manifest = _read_manifest(plan_dir, "giveupnotify")
    assert manifest["stories"]["S1"]["status"] == "failed"
    assert "S1" in result.get("failed", [])
    give_up_notes = [n for n in notes if "S1" in n and "gave up" in n]
    assert give_up_notes, notes
    assert "clarification" in give_up_notes[0]


def test_advance_pipeline_local_failure_terminal_under_local_mode(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Under explicit PIPELINE_BACKEND_DISPATCH=local, a failed local agent is
    terminal — it must NOT escalate to Claude (escalation is an auto-mode
    fallback). This keeps a local-only run from silently spending Claude."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    _write_manifest(plan_dir, "esclocal", {
        "S1": {"summary": "Thing", "agent_instructions": "Build.",
               "status": "in_progress", "pid": 9004,
               "worktree": str(worktree_path),
               "log": str(worktree_path / "agent.log"),
               "backend": "local", "dependencies": []},
    })

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    # See esc1 comment: agent is dead (kill raises, ps empty), test command
    # fails. The new reap path will turn this story's dead pid back to "todo"
    # first — that's why this test asserts the local-failure terminal path,
    # not the reap.
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("esclocal")

    story = _read_manifest(plan_dir, "esclocal")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["backend"] == "local"        # not flipped to claude
    assert not story.get("escalated")          # never escalated
    assert "S1" in result.get("failed", [])


def test_advance_pipeline_retries_on_local_fallback_model(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A plan opted into manifest["local_model_fallback"] retries a failed
    local story on that model instead of parking immediately - and never on
    Claude, even though this is a local-only (not "auto") run, mirroring
    esclocal's terminal guard but with a fallback model configured."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9005,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "dispatched_model": "gpt-oss:20b",
                   "dependencies": []},
        },
    }))

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("escfallback")

    story = _read_manifest(plan_dir, "escfallback")["stories"]["S1"]
    assert story["status"] == "todo"
    assert story["backend"] == "local"                 # never claude
    assert story["model"] == "glm-5.2:cloud"
    assert story["tried_fallback_model"] is True
    assert "pid" not in story
    assert "dispatched_model" not in story
    assert "S1" not in result.get("failed", [])
    notif = (plan_dir / "escfallback.notifications.log").read_text()
    assert "retrying on fallback model glm-5.2:cloud" in notif


def test_advance_pipeline_fallback_model_failure_is_terminal(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Once the fallback model has already been tried (tried_fallback_model),
    a second failure is terminal - it must not retry forever, and must not
    escalate to Claude either."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / "agent.log").write_text("some output\n")
    (plan_dir / "escfallback2.manifest.json").write_text(json.dumps({
        "epics": {}, "local_model_fallback": "glm-5.2:cloud",
        "stories": {
            "S1": {"summary": "Thing", "agent_instructions": "Build.",
                   "status": "in_progress", "pid": 9006,
                   "worktree": str(worktree_path),
                   "log": str(worktree_path / "agent.log"),
                   "backend": "local", "model": "glm-5.2:cloud",
                   "tried_fallback_model": True, "dependencies": []},
        },
    }))

    class _FailResult:
        stdout = "test failed"
        stderr = ""
        returncode = 1

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    def _fake_subprocess(cmd, **kw):
        if cmd and cmd[0] == "ps":
            class _Gone:
                returncode = 1
                stdout = ""
                stderr = ""
            return _Gone()
        return _FailResult()
    monkeypatch.setattr(p.subprocess, "run", _fake_subprocess)
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))

    result = p.advance_pipeline("escfallback2")

    story = _read_manifest(plan_dir, "escfallback2")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["backend"] == "local"         # never claude
    assert "S1" in result.get("failed", [])


# ---------- Interrupt path ----------
def test_interrupt_story_sends_sigterm_and_checkpoints(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "it", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    killed = []
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-int\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.interrupt_story("it", "S1")

    assert killed == [(4242, pcheckpoint.signal.SIGTERM)]
    assert result["ok"] is True
    assert result["status"] == "interrupted"
    assert result["commit"] == "sha-int"

    manifest = _read_manifest(plan_dir, "it")
    story = manifest["stories"]["S1"]
    assert story["status"] == "interrupted"
    assert story["last_commit"] == "sha-int"
    assert "interrupted_at" in story

    journal = json.loads((plan_dir / "it.S1.journal.json").read_text())
    assert journal[-1]["step"] == "interrupted"


def test_interrupt_story_handles_already_dead_process(plan_dir, tmp_path, monkeypatch):
    worktree = str(tmp_path / "wt")
    _write_manifest(plan_dir, "it2", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": worktree},
    })

    def _raise_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(p.os, "kill", _raise_kill)

    def _fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = "sha-dead\n" if cmd[:2] == ["git", "rev-parse"] else ""
            stderr = ""
        return Result()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.interrupt_story("it2", "S1")
    assert result["ok"] is True
    assert result["status"] == "interrupted"
    manifest = _read_manifest(plan_dir, "it2")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_interrupt_story_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "it3", {})
    result = p.interrupt_story("it3", "NOPE")
    assert result["ok"] is False
    assert "NOPE" in result["error"]


def test_interrupt_story_not_dispatched_returns_error(plan_dir):
    _write_manifest(plan_dir, "it4", {
        "S1": {"summary": "thing", "status": "todo"},
    })
    result = p.interrupt_story("it4", "S1")
    assert result["ok"] is False
    assert "not dispatched" in result["error"].lower()


def test_advance_pipeline_retries_review_for_orphaned_tests_passed_story(plan_dir, monkeypatch):
    """A story stuck at tests_passed (e.g. review_story crashed mid-tick on
    a prior run) must be retried on the next tick, not silently ignored."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "orphan", {
        "S1": {"summary": "thing", "status": "tests_passed",
               "worktree": "/x", "risk": "low"},
    })

    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )

    result = p.advance_pipeline("orphan")
    assert reviewed == ["S1"]
    assert {"S1": "pr_open"} in result["advanced"]


# ---------- Fix #1: harness-owned acceptance oracle ----------

def test_ingest_plan_round_trips_acceptance_field(
    plan_dir, monkeypatch, tmp_path,
):
    """`acceptance` is optional; when present on a source story it must be
    carried verbatim onto the manifest entry so dispatch_story can later
    forward it to the local driver."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing",
             "agent_instructions": "Build.",
             "acceptance": [
                 {"path": "tests/test_x.py", "source": "import pytest\n"},
             ]},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    story = manifest["stories"]["S1"]
    assert story["acceptance"] == [
        {"path": "tests/test_x.py", "source": "import pytest\n"},
    ]


def test_ingest_plan_omits_acceptance_when_source_story_has_none(
    plan_dir, monkeypatch, tmp_path,
):
    """Backwards compat: stories without an acceptance block still work and
    end up with an empty acceptance list on the manifest."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing"},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["acceptance"] == []


def test_ingest_plan_round_trips_tdd_split_opt_in(plan_dir, monkeypatch, tmp_path):
    """§2.4's story-level eligibility gate: an explicit story["tdd_split"]
    opt-in must survive ingest onto the manifest, since dispatch_story reads
    it from there, not from the plan JSON."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build.",
             "tdd_split": True},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["tdd_split"] is True


def test_ingest_plan_defaults_tdd_split_to_false(plan_dir, monkeypatch, tmp_path):
    """Absent opt-in must default False - Secure Defaults, and matches
    dispatch_story's `story.get("tdd_split")` truthiness check."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing"},
        ]}],
        "repo_root": str(tmp_path),
    }))

    p.ingest_plan("p")

    story = json.loads((plan_dir / "p.manifest.json").read_text())["stories"]["S1"]
    assert story["tdd_split"] is False


# ---------- role_config propagation bug: ingest_plan silently drops it ----------

def test_ingest_plan_writes_role_config_into_manifest(plan_dir, monkeypatch, tmp_path):
    """A plan-level role_config block (documented alongside epics/stories -
    see save_plan's docstring and the README's "Per-role provider/model
    configuration" section) must land on the manifest, since
    _plan_role_config() only ever reads it from there, never from the plan
    JSON."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))

    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "review": {"provider": "claude", "model": "sonnet"},
    }


def test_ingest_plan_updates_role_config_on_reingest(plan_dir, monkeypatch, tmp_path):
    """Re-ingesting (merge mode, default overwrite=False) with a DIFFERENT
    role_config must fully replace the old one - role_config is an authored
    field refreshed on ingest, same as epics/stories, not deep-merged."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"overlord": {"provider": "ollama", "model": "devstral"}},
    }))
    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "overlord": {"provider": "ollama", "model": "devstral"},
    }


def test_ingest_plan_preserves_role_config_when_absent_on_reingest(
    plan_dir, monkeypatch, tmp_path,
):
    """A re-ingest whose plan JSON has NO role_config key at all (e.g. a
    caller that only re-authors epics/stories) must leave the manifest's
    existing role_config untouched, not wipe it to {} - matching the
    documented contract that "top-level manifest keys outside
    epics/stories/repo_root ... carry over untouched"."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build. v2"},
        ]}],
        "repo_root": str(tmp_path),
    }))
    p.ingest_plan("p")

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {
        "review": {"provider": "claude", "model": "sonnet"},
    }


def test_ingest_plan_overwrite_true_drops_role_config_when_absent(
    plan_dir, monkeypatch, tmp_path,
):
    """overwrite=True restores the old wholesale-replace behavior (drops
    anything not produced by this call) - so a re-ingest with overwrite=True
    and no role_config key must reset the manifest's role_config to {},
    matching how it already drops un-repeated stories/epics."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    plan_path = plan_dir / "p.json"
    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "sonnet"}},
    }))
    p.ingest_plan("p")

    plan_path.write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
    }))
    p.ingest_plan("p", overwrite=True)

    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    assert manifest["role_config"] == {}


def test_get_role_config_reports_ingested_role_config_override(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """The actual user-visible symptom: get_role_config(plan_name=...) must
    report the role_config authored in the plan and carried onto the
    manifest by ingest_plan, not the env/registry/persona default - verified
    via the tool function directly, not just the raw manifest dict.

    The plan pins review's model to "opus", deliberately different from the
    agents_dir fixture's code-reviewer.md ("sonnet", the fallback that wins
    when no role_config reaches the manifest), so a pre-fix run reports the
    fallback and mismatches this assertion instead of passing by
    coincidence."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)
    (plan_dir / "p.json").write_text(json.dumps({
        "epics": [{"summary": "Epic", "stories": [
            {"key": "S1", "summary": "Do thing", "agent_instructions": "Build."},
        ]}],
        "repo_root": str(tmp_path),
        "role_config": {"review": {"provider": "claude", "model": "opus"}},
    }))

    p.ingest_plan("p")

    result = p.get_role_config(plan_name="p")
    assert result["roles"]["review"] == {"provider": "claude", "model": "opus"}


def test_dispatch_story_writes_oracle_files_into_worktree(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When a story carries `acceptance`, dispatch_story materializes each
    oracle file at its declared path inside the worktree BEFORE invoking the
    backend, so the local oracle-harness can grade against it on launch."""
    _write_manifest(plan_dir, "oracle_fresh", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(4242))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_fresh", "S1")

    wt = worktree_root / "S1"
    assert (wt / "tests/test_x.py").exists()
    assert (wt / "tests/test_x.py").read_text() == "import pytest\n"


def test_dispatch_story_forwards_acceptance_paths_to_local_driver(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With `dispatch=local`, the local driver receives the acceptance paths
    as JSON via LOCAL_AGENT_ACCEPTANCE and is launched in oracle mode."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "oracle_env", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
                   {"path": "tests/test_y.py", "source": "import pytest\n"},
               ]},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7777)

    # This story carries an `acceptance` block, so the pre-dispatch oracle
    # gate needs a real CompletedProcess from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_env", "S1")

    assert len(popen_calls) == 1
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_MODE"] == "oracle"
    assert json.loads(env["LOCAL_AGENT_ACCEPTANCE"]) == [
        "tests/test_x.py", "tests/test_y.py",
    ]


def test_dispatch_story_forwards_acceptance_paths_under_explicit_provider_name(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T16: an explicitly-pinned provider name (e.g. "lmstudio"), not just
    the "local" alias, must also count as local-family for the acceptance
    passthrough gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")
    _write_manifest(plan_dir, "oracle_env_lmstudio", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7778)

    # This story carries an `acceptance` block, so the pre-dispatch oracle
    # gate needs a real CompletedProcess from subprocess.run, not None.
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "assert 0", ""),
    )
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_env_lmstudio", "S1")

    assert len(popen_calls) == 1
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_MODE"] == "oracle"
    assert json.loads(env["LOCAL_AGENT_ACCEPTANCE"]) == ["tests/test_x.py"]


def test_dispatch_story_omits_oracle_env_when_no_acceptance(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Regression guard: stories without an `acceptance` block must keep
    exactly the same env-var surface as before — no LOCAL_AGENT_ACCEPTANCE,
    no LOCAL_AGENT_MODE, no oracle script. Otherwise every existing plan
    silently switches harnesses."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "no_oracle", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "todo", "dependencies": []},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(8888)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("no_oracle", "S1")

    # test_author (claude/sonnet) issues its own leading Popen call first;
    # the executor's (local, oracle-relevant) call is the last one.
    env = popen_calls[-1]["env"]
    assert "LOCAL_AGENT_ACCEPTANCE" not in env
    assert "LOCAL_AGENT_MODE" not in env
    # base script (not the oracle variant)
    assert popen_calls[-1]["cmd"][1].endswith("scripts/local_agent.py")


def test_dispatch_story_skips_oracle_write_when_resumed(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Resumed/interrupted stories must NOT have their oracle files
    re-overwritten — the agent may have committed an evolved oracle in a WIP
    and we don't want to silently revert it."""
    wt = worktree_root / "S1"
    wt.mkdir()
    (wt / "tests").mkdir()
    (wt / "tests/test_x.py").write_text("# evolved by the agent\n")
    _write_manifest(plan_dir, "oracle_resume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build.",
               "status": "interrupted",
               "dependencies": [],
               "acceptance": [
                   {"path": "tests/test_x.py", "source": "import pytest\n"},
               ]},
    })

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1234))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("oracle_resume", "S1")

    assert (wt / "tests/test_x.py").read_text() == "# evolved by the agent\n"


def test_dispatch_story_local_rework_resumes_transcript_when_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch on the local Ollama driver whose worktree already
    holds a transcript from the prior attempt must resume that transcript
    (LOCAL_AGENT_RESUME_TRANSCRIPT_PATH + LOCAL_AGENT_RESUME_APPEND_CONTENT)
    instead of rebuilding the from-scratch rework_instruction prompt."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    # Always-on: the rework planner now runs for local-family dispatch. This
    # test pins the transcript-resume path and the raw-feedback append format
    # (the planner fail-open case), so stub the planner to None rather than
    # reaching a live backend.
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6001)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("localrw", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(transcript_path)
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )
    # The old-style rework_instruction must not be baked into the cold-start
    # task prompt in this path - the transcript already carries prior context
    # and the append content carries the new feedback.
    assert "REQUESTED CHANGES" not in env["LOCAL_AGENT_TASK"]
    assert "The SQL is injectable" not in env["LOCAL_AGENT_TASK"]


def test_dispatch_story_explicit_provider_rework_resumes_transcript_when_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """T16: an explicitly-pinned provider name (e.g. "lmstudio"), not just
    the "local" alias, must also count as local-family for the
    transcript-resume-on-rework gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "lmstudio")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "lmstudiorw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6005)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("lmstudiorw", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(transcript_path)


def test_dispatch_story_local_rework_surfaces_revised_agent_instructions(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A transcript-resume rework redispatch must surface an operator's
    patch_story edit to agent_instructions since the story's last dispatch -
    otherwise the resumed agent only ever sees the reviewer's raw feedback
    and a corrected instruction (e.g. "delete the redundant retry wrapper
    instead of adding a new one") is silently dropped, and the agent
    re-derives its own (possibly wrong) fix instead of the one it was given.
    Detected via `_dispatched_agent_instructions`, a snapshot this same
    function records on every dispatch (see the sibling
    test_dispatch_story_local_rework_records_dispatched_instructions_snapshot)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwrevised", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "Delete the redundant retry wrapper in foo().",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6004)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwrevised", "S1")

    env = popen_calls[0]["env"]
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "Delete the redundant retry wrapper in foo()." in append
    assert "revised" in append.lower() or "updated" in append.lower()
    # The reviewer's raw feedback must still be present too.
    assert "The SQL is injectable; parameterize it." in append


def test_dispatch_story_local_rework_omits_note_when_instructions_unchanged(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When agent_instructions is identical to the snapshot recorded at the
    story's last dispatch, no "revised instructions" note is injected - the
    append content is exactly the existing feedback-only format. Guards
    against re-surfacing the same instructions (as noise) on every rework
    round when nothing was actually patched."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwsame", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6006)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwsame", "S1")

    env = popen_calls[0]["env"]
    assert env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )


def test_dispatch_story_records_dispatched_instructions_snapshot(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Every dispatch_story call (cold or resumed) must record the
    agent_instructions it just handed the agent as
    story["_dispatched_agent_instructions"], persisted to the manifest - the
    baseline the next rework redispatch diffs against to detect an
    operator's patch_story edit."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    manifest_path = plan_dir / "localrwsnap.manifest.json"
    _write_manifest(plan_dir, "localrwsnap", {
        "S1": {"summary": "Do thing",
               "agent_instructions": "Delete the redundant retry wrapper in foo().",
               "_dispatched_agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env, **kw: _FakeProc(6007))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwsnap", "S1")

    saved = json.loads(manifest_path.read_text())
    assert saved["stories"]["S1"]["_dispatched_agent_instructions"] == (
        "Delete the redundant retry wrapper in foo()."
    )


def test_dispatch_story_local_rework_falls_back_when_transcript_missing(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """If the worktree has no transcript file (first dispatch predated this
    feature, ran on a different backend, or the file was cleaned up), the
    local-driver rework redispatch must fall back to the existing
    from-scratch rework_instruction prompt rather than crash."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    _write_manifest(plan_dir, "localrwmiss", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6002)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("localrwmiss", "S1")

    assert result["resumed"] is True
    env = popen_calls[0]["env"]
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in env
    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in env
    assert "The SQL is injectable; parameterize it." in env["LOCAL_AGENT_TASK"]


def test_dispatch_story_local_rework_empty_review_feedback_skips_resume_path(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An empty review_feedback string is falsy, matching the existing
    rework_instruction guard (`if review_feedback:`) - it must not trigger
    the transcript-resume path even when a transcript file exists."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "localrwempty", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": ""},
    })

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(6003)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwempty", "S1")

    env = popen_calls[0]["env"]
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in env
    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in env


def test_dispatch_story_claude_rework_unaffected_by_transcript_resume(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The claude backend's rework redispatch must be completely unchanged by
    this feature: it never sees LOCAL_AGENT_* env vars and still builds the
    full from-scratch rework prompt via _build_dispatch_command, even when a
    transcript file happens to exist in the worktree (e.g. left over from a
    prior local-backend attempt before an escalation flip)."""
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "clauderw", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    popen_calls = []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: popen_calls.append(cmd) or _FakeProc(6004))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("clauderw", "S1")

    assert result["resumed"] is True
    prompt = popen_calls[0][popen_calls[0].index("-p") + 1]
    assert "The SQL is injectable; parameterize it." in prompt
    assert "REQUESTED CHANGES" in prompt


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

_RATE_LIMIT_MSG = (
    "You've hit your session limit · resets 8:20pm (America/Chicago)"
)

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
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

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
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

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


# ---------- Non-rate-limited UNKNOWN must not burn the rework budget ----------

def test_review_story_unknown_leaves_rework_and_feedback_untouched(plan_dir, agents_dir, monkeypatch):
    # A genuinely inconclusive (non-rate-limited) UNKNOWN verdict must not be
    # treated like REQUEST_CHANGES: no rework_attempts, no review_feedback
    # (which would otherwise redispatch the agent blind on empty feedback),
    # and no changes_requested status.
    _write_manifest(plan_dir, "unk_untouched", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on UNKNOWN")))

    result = p.review_story("unk_untouched", "S1")

    assert result["verdict"] == "UNKNOWN"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "unk_untouched")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert "rework_attempts" not in story
    assert "review_feedback" not in story
    assert story["review_inconclusive_count"] == 1


def test_review_story_unknown_notifies_user_will_retry(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "unk_notify", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.review_story("unk_notify", "S1")

    assert any("inconclusive" in n.lower() for n in notes)


def test_review_story_unknown_parks_after_max_inconclusive_attempts(plan_dir, agents_dir, monkeypatch):
    # Default max is 2: a second consecutive UNKNOWN must park the story for
    # human review rather than retrying forever - and must never reach
    # APPROVE/pr_open. review_verdict stays UNKNOWN throughout.
    _write_manifest(plan_dir, "unk_park", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    pr_calls = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: pr_calls.append(1))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result1 = p.review_story("unk_park", "S1")
    assert result1["status"] == "tests_passed"

    result2 = p.review_story("unk_park", "S1")

    assert result2["verdict"] == "UNKNOWN"
    assert result2["status"] == "parked"
    story = _read_manifest(plan_dir, "unk_park")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["review_verdict"] == "UNKNOWN"
    assert story["review_inconclusive_count"] == 2
    assert "inconclusive after 2 attempts" in story["parked_reason"]
    assert pr_calls == [], "an UNKNOWN verdict must never open a PR"
    assert any("parked" in n.lower() for n in notes)


def test_review_story_conclusive_verdict_after_unknown_resets_and_reworks(plan_dir, agents_dir, monkeypatch):
    # A real REQUEST_CHANGES following a prior UNKNOWN must carry the actual
    # feedback, start rework_attempts fresh from 0 (the UNKNOWN must not have
    # silently pre-incremented it), and clear the inconclusive counter.
    _write_manifest(plan_dir, "unk_then_real", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, **k):
        calls.append(1)
        if len(calls) == 1:
            return "no verdict line here"
        return "The error path is untested.\nVERDICT: REQUEST_CHANGES"

    monkeypatch.setattr(p, "_run_reviewer", _stub)

    result1 = p.review_story("unk_then_real", "S1")
    assert result1["verdict"] == "UNKNOWN"
    story = _read_manifest(plan_dir, "unk_then_real")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1

    result2 = p.review_story("unk_then_real", "S1")

    assert result2["verdict"] == "REQUEST_CHANGES"
    assert result2["status"] == "changes_requested"
    story = _read_manifest(plan_dir, "unk_then_real")["stories"]["S1"]
    assert story["review_feedback"] == "The error path is untested.\nVERDICT: REQUEST_CHANGES"
    assert story["rework_attempts"] == 1
    assert story["review_inconclusive_count"] == 0


def test_review_story_unknown_rate_limited_still_defers_not_inconclusive(plan_dir, agents_dir, monkeypatch):
    # Regression: a rate-limited UNKNOWN must keep taking the existing FM-B
    # deferral path, not the new inconclusive-retry path - it must not
    # increment review_inconclusive_count at all.
    _write_manifest(plan_dir, "unk_rl_regression", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))

    result = p.review_story("unk_rl_regression", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "unk_rl_regression")["stories"]["S1"]
    assert "review_inconclusive_count" not in story
    assert "rework_attempts" not in story


def test_review_story_unknown_inconclusive_max_one_parks_on_first_attempt(plan_dir, agents_dir, monkeypatch):
    # Boundary: PIPELINE_REVIEW_INCONCLUSIVE_MAX=1 parks on the very first
    # inconclusive verdict rather than waiting for a second.
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 1)
    _write_manifest(plan_dir, "unk_max_one", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on UNKNOWN")))

    result = p.review_story("unk_max_one", "S1")

    assert result["status"] == "parked"
    story = _read_manifest(plan_dir, "unk_max_one")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["review_inconclusive_count"] == 1


# ---------- Escalate-to-Claude on rework/inconclusive exhaustion (auto mode) ----------
#
# Turning the review-side loop 100% autonomous means the two remaining
# park-for-a-human paths (rework budget exhausted, review inconclusive
# exhausted) need a fallback too - PIPELINE_BACKEND_DISPATCH=auto already
# escalates a failed DISPATCH to Claude; these tests extend the same
# philosophy to a local reviewer that can't converge. Unlike the dispatch
# escalation, this does NOT wipe the worktree/branch - the existing code is
# very often already correct (this session's benchmark runs showed most of
# these parks hold ground-truth-correct implementations a local reviewer
# just couldn't cleanly resolve), so Claude reviews/reworks the SAME
# worktree in place rather than starting over.

def test_review_story_rework_exhausted_escalates_to_claude_under_auto(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvesc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvesc", "S1")

    story = _read_manifest(plan_dir, "rvesc")["stories"]["S1"]
    assert story["status"] != "parked"
    assert story["status"] == "changes_requested"
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    # Fresh budget for Claude - the local count must not carry over and
    # silently exhaust immediately on the very next cycle.
    assert "rework_attempts" not in story
    assert result["status"] == "changes_requested"


def test_review_story_rework_exhausted_parks_when_already_escalated(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression/terminal guard: a story already escalated (i.e. Claude
    itself is now failing to satisfy review) must park for real - there is
    no further fallback past Claude, so this must not loop forever."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvescdone", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True, "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvescdone", "S1")

    story = _read_manifest(plan_dir, "rvescdone")["stories"]["S1"]
    assert story["status"] == "parked"
    assert result["status"] == "parked"


def test_review_story_rework_exhausted_parks_when_auto_disabled(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression guard: without PIPELINE_BACKEND_DISPATCH=auto, behavior is
    unchanged from before this story - park for a human, no escalation."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvnoauto", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvnoauto", "S1")

    story = _read_manifest(plan_dir, "rvnoauto")["stories"]["S1"]
    assert story["status"] == "parked"
    assert "escalated" not in story
    assert result["status"] == "parked"


def test_review_story_inconclusive_exhausted_escalates_to_claude_under_auto(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 2)
    _write_manifest(plan_dir, "unkesc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "review_inconclusive_count": 1},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")

    result = p.review_story("unkesc", "S1")

    story = _read_manifest(plan_dir, "unkesc")["stories"]["S1"]
    assert story["status"] != "parked"
    assert story["status"] == "tests_passed"
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert "review_inconclusive_count" not in story
    assert result["status"] == "tests_passed"


def test_review_story_inconclusive_exhausted_parks_when_already_escalated(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 2)
    _write_manifest(plan_dir, "unkescdone", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True,
               "review_inconclusive_count": 1},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "no verdict line here")

    result = p.review_story("unkescdone", "S1")

    story = _read_manifest(plan_dir, "unkescdone")["stories"]["S1"]
    assert story["status"] == "parked"
    assert result["status"] == "parked"


def test_review_story_escalated_story_reviews_via_claude_backend(
    plan_dir, agents_dir, monkeypatch,
):
    """Once escalated, EVERY subsequent review call for that story must go
    to Claude regardless of the global PIPELINE_BACKEND_REVIEW setting -
    review is normally resolved purely from the env var, with no per-story
    override, so this is the one seam that must explicitly check
    story['escalated'] and force backend_name='claude'."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    _write_manifest(plan_dir, "escreview", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True},
    })
    captured = {}

    def _fake_reviewer(wt, br, backend_name=None, **k):
        captured["backend_name"] = backend_name
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("escreview", "S1")

    assert captured["backend_name"] == "claude"


def test_review_story_escalated_story_reviews_via_escalation_target(
    plan_dir, agents_dir, monkeypatch,
):
    """PIPELINE_ESCALATION_BACKEND retargets the escalated-review seam away from
    Claude: once a story is escalated and the operator has retargeted
    escalation to a non-Claude backend, every subsequent review for that story
    must go to the escalation target - not hardcoded Claude (which may be
    usage-capped and unavailable). Default (env unset) still forces Claude,
    as the prior test asserts."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    _write_manifest(plan_dir, "escrevtgt", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True},
    })
    captured = {}

    def _fake_reviewer(wt, br, backend_name=None, **k):
        captured["backend_name"] = backend_name
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("escrevtgt", "S1")

    assert captured["backend_name"] == "ollama"


# ---------- FM-A: acceptance oracle gates check_story_status when present ----------

def _setup_oracle_story(plan_dir, plan_name, worktree, acceptance=None, extra=None):
    """Write a manifest story with the given acceptance block and worktree."""
    story = {
        "summary": "Implement thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
    }
    if acceptance is not None:
        story["acceptance"] = acceptance
    if extra:
        story.update(extra)
    _write_manifest(plan_dir, plan_name, {"S1": story})


def test_check_story_status_with_acceptance_runs_only_oracle_tests(plan_dir, monkeypatch):
    # FM-A: when a story has an acceptance block, check_story_status must run
    # only the oracle test files — not the model's self-written tests.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")

    acceptance = [{"path": "tests/test_oracle.py", "source": "def test_ok(): pass"}]
    _setup_oracle_story(plan_dir, "fm_a_oracle", worktree, acceptance=acceptance)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    captured_cmds = []

    class _Pass:
        returncode = 0
        stdout = "1 passed"

    def _fake_run(cmd, **kw):
        captured_cmds.append(list(cmd))
        return _Pass()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_oracle", "S1")

    assert result["status"] == "tests_passed"
    # The command actually run must include the oracle path, not be a bare suite run.
    test_cmds = [c for c in captured_cmds if "pytest" in c[0] or "pytest" in (c[1] if len(c) > 1 else "")]
    assert test_cmds, "pytest must have been called"
    assert any("tests/test_oracle.py" in " ".join(cmd) for cmd in captured_cmds), (
        "oracle path must appear in the pytest command when acceptance is set"
    )


def test_check_story_status_with_acceptance_fails_when_oracle_fails(plan_dir, monkeypatch):
    # When the oracle tests fail, status must be `failed` even if the model's
    # own tests would pass.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")

    acceptance = [{"path": "tests/test_oracle.py", "source": "def test_spec(): assert False"}]
    _setup_oracle_story(plan_dir, "fm_a_fail", worktree, acceptance=acceptance)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_fail", "S1")
    assert result["status"] == "failed"


def test_check_story_status_without_acceptance_runs_whole_suite(plan_dir, monkeypatch):
    # Regression: stories without an acceptance block must still run the full suite.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")
    _setup_oracle_story(plan_dir, "fm_a_nosuite", worktree, acceptance=None)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    captured_cmds = []

    class _Pass:
        returncode = 0
        stdout = "all passed"

    def _fake_run(cmd, **kw):
        captured_cmds.append(list(cmd))
        return _Pass()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_nosuite", "S1")

    assert result["status"] == "tests_passed"
    test_cmds = [c for c in captured_cmds if "pytest" in c[0] or (len(c) > 1 and "pytest" in c[1])]
    # Must not have scoped to any specific file (no path args beyond bare pytest).
    assert any(c == ["pytest"] for c in test_cmds), (
        "whole-suite run must be bare pytest when no acceptance block"
    )


# ---------- issue 24eb6c5b: lock approve_merge + refresh parked_reason ----------

def test_approve_merge_returns_retriable_busy_when_lock_held(plan_dir, monkeypatch):
    """approve_merge must not proceed on stale state when the plan lock is
    already held by another context (scheduler tick). It returns a clean,
    retriable error instead of crashing or silently succeeding."""
    _write_manifest(plan_dir, "ambusy", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    lock_path = plan_dir / "ambusy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.approve_merge("ambusy", "P1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is False
    assert result.get("retriable") is True
    assert "busy" in result["error"].lower() or "retry" in result["error"].lower()
    assert merged == []
    # Story must be untouched.
    assert _read_manifest(plan_dir, "ambusy")["stories"]["P1"]["status"] == "parked"


def test_approve_merge_rereads_manifest_inside_lock(plan_dir, monkeypatch):
    """approve_merge must re-read the manifest from disk AFTER acquiring the
    lock, so it merges against the freshest on-disk state, not a pre-lock
    stale copy. We mutate an unrelated field on disk before the call and
    confirm the merge proceeds using the fresh manifest (the worktree path
    is taken from the on-disk manifest, so we change it and verify the
    merge used the fresh value)."""
    _write_manifest(plan_dir, "amfresh", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/old/wt"},
    })

    seen_worktrees = []
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, sha: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})

    def _capture_merge(wt, key):
        seen_worktrees.append(wt)
        return "merged"
    monkeypatch.setattr(p, "_merge_pr", _capture_merge)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # Mutate the on-disk manifest to a fresh worktree path BEFORE calling
    # approve_merge. If approve_merge uses a pre-lock stale copy, it will
    # pass "/old/wt" to _merge_pr; if it re-reads inside the lock, it will
    # pass "/fresh/wt".
    _write_manifest(plan_dir, "amfresh", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/fresh/wt"},
    })

    result = p.approve_merge("amfresh", "P1")

    assert result["ok"] is True
    assert seen_worktrees == ["/fresh/wt"]


def test_approve_merge_revalidates_status_inside_lock(plan_dir, monkeypatch):
    """If the story's status changed on disk while waiting for the lock
    (e.g. a scheduler tick already merged it), approve_merge must detect the
    stale state and return the validation error rather than proceeding."""
    _write_manifest(plan_dir, "amstale", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # Simulate the on-disk state changing to 'done' before approve_merge
    # acquires the lock (the pre-lock read sees 'parked', but the in-lock
    # re-read sees 'done').
    _write_manifest(plan_dir, "amstale", {
        "P1": {"summary": "approved", "status": "done", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })

    result = p.approve_merge("amstale", "P1")

    assert result["ok"] is False
    assert merged == []


def test_approve_merge_revalidates_review_verdict_inside_lock(plan_dir, monkeypatch):
    """If the review_verdict changed on disk while waiting for the lock,
    approve_merge must detect it and refuse rather than merging unapproved work."""
    _write_manifest(plan_dir, "amverdict", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # On-disk verdict changed to REQUEST_CHANGES before the lock was acquired.
    _write_manifest(plan_dir, "amverdict", {
        "P1": {"summary": "changes requested", "status": "parked",
               "review_verdict": "REQUEST_CHANGES", "risk": "medium", "worktree": "/x"},
    })

    result = p.approve_merge("amverdict", "P1")

    assert result["ok"] is False
    assert merged == []


def test_merge_gate_park_sets_parked_reason(plan_dir, monkeypatch):
    """When the scheduler's merge-adjudication loop parks a pr_open story
    (non-merge decision), it must set parked_reason to the decision's reason
    string, matching the review-park sites."""
    _write_manifest(plan_dir, "mgpark", {
        "P1": {"summary": "approved but high risk", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    # advance_pipeline will adjudicate the merge; high risk -> park.
    p.advance_pipeline("mgpark")

    story = _read_manifest(plan_dir, "mgpark")["stories"]["P1"]
    assert story["status"] == "parked"
    assert story.get("parked_reason") == "high risk held for human review"


def test_approve_merge_clears_parked_reason_on_done(plan_dir, monkeypatch):
    """A story leaving 'parked' status via successful approve_merge must no
    longer carry a stale parked_reason key."""
    _write_manifest(plan_dir, "amclear", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x", "parked_reason": "high risk held for human review"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("amclear", "P1")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "amclear")["stories"]["P1"]
    assert story["status"] == "done"
    assert "parked_reason" not in story


def test_scheduler_merge_clears_parked_reason_on_done(plan_dir, monkeypatch):
    """When the scheduler's merge loop successfully merges a pr_open story
    that previously carried a parked_reason, the reason must be cleared."""
    _write_manifest(plan_dir, "smclear", {
        "P1": {"summary": "approved low risk", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x",
               "parked_reason": "stale reason from a prior park"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, sha: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    p.advance_pipeline("smclear")

    story = _read_manifest(plan_dir, "smclear")["stories"]["P1"]
    assert story["status"] == "done"
    assert "parked_reason" not in story


def test_set_story_status_clears_parked_reason_on_unpark(plan_dir, monkeypatch):
    """set_story_status transitioning a story OUT of 'parked' to an active
    status must clear parked_reason so a stale reason doesn't survive."""
    _write_manifest(plan_dir, "sss", {
        "P1": {"summary": "parked", "status": "parked",
               "parked_reason": "high risk held for human review"},
    })

    result = p.set_story_status("sss", "P1", "interrupted")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "sss")["stories"]["P1"]
    assert story["status"] == "interrupted"
    assert "parked_reason" not in story


# ---------- Auto-retry review on transient backend 500 ----------

_TRANSIENT_500_MSG = "500 Internal Server Error: upstream crashed mid-request"


def test_is_transient_backend_error_detects_500():
    assert p._is_transient_backend_error("HTTP 500 Internal Server Error")


def test_is_transient_backend_error_detects_internal_server_error():
    assert p._is_transient_backend_error("internal server error: something broke")


def test_is_transient_backend_error_detects_connection_reset():
    assert p._is_transient_backend_error("Connection reset by peer")


def test_is_transient_backend_error_detects_connection_refused():
    assert p._is_transient_backend_error("Connection refused while contacting backend")


def test_is_transient_backend_error_false_for_rate_limit_banner():
    """The two detectors must not double-handle the same input."""
    assert not p._is_transient_backend_error(_RATE_LIMIT_MSG)


def test_is_transient_backend_error_false_for_normal_review():
    normal = (
        "I reviewed the diff. The implementation looks correct.\n"
        "VERDICT: APPROVE\n"
    )
    assert not p._is_transient_backend_error(normal)


def test_is_rate_limited_false_for_transient_500():
    """Conversely, a transient-500 must not be treated as a rate-limit."""
    assert not p._is_rate_limited(_TRANSIENT_500_MSG)


def test_review_story_transient_500_retry_resolves_to_approve(
    plan_dir, agents_dir, monkeypatch
):
    """First reviewer call returns a transient-500 UNKNOWN; the single retry
    returns VERDICT: APPROVE. The story must end up approved with
    review_inconclusive_count NOT incremented."""
    _write_manifest(plan_dir, "transient_ok", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _TRANSIENT_500_MSG
        return "Looks good.\nVERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("transient_ok", "S1")

    assert result["verdict"] == "APPROVE"
    assert call_count["n"] == 2  # original + one retry
    story = _read_manifest(plan_dir, "transient_ok")["stories"]["S1"]
    assert story["review_verdict"] == "APPROVE"
    assert story.get("review_inconclusive_count", 0) == 0


def test_review_story_transient_500_retry_also_fails_increments_inconclusive_once(
    plan_dir, agents_dir, monkeypatch
):
    """Both the original and the retry return transient-500 UNKNOWN. The retry
    must happen exactly once (2 total calls) and review_inconclusive_count
    must increment by exactly 1, falling through to the existing inconclusive
    path unchanged."""
    _write_manifest(plan_dir, "transient_fail", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        return _TRANSIENT_500_MSG  # always fails

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("transient_fail", "S1")

    assert call_count["n"] == 2  # original + exactly one retry
    assert result["verdict"] == "UNKNOWN"
    story = _read_manifest(plan_dir, "transient_fail")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1  # incremented exactly once
    assert story["status"] == "tests_passed"


# ---------- Guided decomposition (GUIDED_DECOMPOSITION_PLAN.md) ----------
# A "tech lead" planner call that turns a coarse story into an ordered
# sub-step checklist for the weak local executor to follow within a single
# worktree/transcript. _run_planner() is a bounded, single complete() call
# (never an agent loop); dispatch_story() gates it behind PIPELINE_DECOMPOSE
# (default "off") and only for local-family backends.

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


def test_run_planner_routes_to_claude_backend_and_passes_system_prompt(
    agents_dir, monkeypatch,
):
    """_run_planner routes to the provider _resolve_planner_backend picks
    (here claude, via PIPELINE_BACKEND_PLANNER) and passes the agent
    instructions as the prompt with _PLANNER_SYSTEM as the system prompt.
    The model is registry/env-driven (covered in test_always_on_planner.py);
    this test pins the call shape, not the model tag."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    fake = _FakePlannerBackend(response="1. Write a failing test\n2. Implement it")
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result == "1. Write a failing test\n2. Implement it"
    assert calls == [{"role": "planner", "name": "claude"}]
    assert fake.calls[0]["prompt"] == "Add a rate limiter."
    assert fake.calls[0]["system"] == p._PLANNER_SYSTEM


def test_run_planner_returns_none_on_backend_failure(agents_dir, monkeypatch):
    """A broken/unreachable planner backend must fail open, not raise -
    dispatch_story must be able to proceed with no plan."""
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


def test_run_planner_returns_none_on_empty_response(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response="   \n  ")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


def test_planner_system_steers_away_from_editing_test_files():
    """The tech-lead checklist must steer the weak executor to put all
    implementation work in the implementation file and never edit the test
    files. Without this, a weak model that lands an incomplete stub spends
    its whole budget editing the test file instead of completing the
    implementation (observed live: lru_cache t4, temp 1.0, 56 consecutive
    str_replace calls on test_lru_cache.py, never implementing get/put/size).
    The steering rule reaches the agent verbatim because _PLANNER_SYSTEM
    instructs the planner to make it the checklist's first line."""
    assert "implementation file" in p._PLANNER_SYSTEM
    assert "test_" in p._PLANNER_SYSTEM
    # The rule must forbid editing tests AND direct fixes to the impl, or a
    # weak model will keep mutating tests to make them pass.
    assert "NEVER" in p._PLANNER_SYSTEM or "never" in p._PLANNER_SYSTEM.lower()
    # The weak model reliably fails surgical str_replace (observed live in
    # t2/t5: every str_replace in a stubs-then-edit loop is rejected as
    # 'old_str occurs N times' / 'not found'). The checklist must direct the
    # executor to write COMPLETE files via create_file instead of
    # stubs-then-surgically-edit, or the model never gets past stubs.
    assert "create_file" in p._PLANNER_SYSTEM
    assert "NotImplementedError" in p._PLANNER_SYSTEM

def test_planner_system_exception_for_small_edits():
    """The planner should allow str_replace for small targeted edits."""
    assert "str_replace" in p._PLANNER_SYSTEM
    assert "preserve" in p._PLANNER_SYSTEM


def test_planner_system_prescribes_delegate_wrapper_for_large_function_edits():
    """Root cause diagnosed live (2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD,
    8 failed dispatch attempts): the story asked the executor to wrap a
    ~300-line existing function's ENTIRE body in a new `with` block - an
    in-place mass re-indent through a truncated view_file/create_file tool,
    the mechanically hardest edit shape for this engine. The identical
    pattern (guard + delegate to a renamed `_foo_impl`) already existed 350
    lines away in the same file for exactly this situation, but nothing
    steered the executor (or the story author) toward it - one attempt tried
    it anyway and botched the split (duplicate defs, orphaned fragments) from
    getting no guidance on the mechanics. Neither existing EDITING MECHANICS
    branch (whole-file create_file rewrite, or str_replace for a small
    preserve-most edit) fits a large-function in-place wrap; the checklist
    must name the rename-and-delegate shape as the correct move for it."""
    assert "_impl" in p._PLANNER_SYSTEM
    assert "delegate" in p._PLANNER_SYSTEM.lower()


def test_planner_system_delegate_wrapper_specifies_what_to_preserve():
    """Root cause diagnosed live (2026-07-22/23, MODE-29-REVIEW-STORY-LOCK-GUARD
    redispatch): the rename-and-delegate recipe told the executor to rename
    `foo` to `_foo_impl` and define a new short `foo` that delegates, but
    never said what the new `foo` must carry over from the original. Every
    Blocking finding across two full review cycles traced to this gap - the
    `@mcp.tool()` decorator was left on the renamed `_foo_impl` (silently
    deregistering the real MCP entrypoint even though tests calling the bare
    module attribute passed), the docstring moved with it (emptying the
    tool's client-facing description), and argument validation ended up
    running inside `_foo_impl` - after the new wrapper's lock/guard setup
    instead of before it, opening a path-traversal window. The recipe must
    name all three explicitly."""
    text = p._PLANNER_SYSTEM.lower()
    assert "decorator" in text
    assert "docstring" in text
    assert "valid" in text and "before" in text


def test_planner_system_worked_examples_must_verify_persisted_state():
    """Live-discovered bug (2026-07-16, production-config benchmark run,
    token_bucket via glm-5.2:cloud/Ollama planner + mlx implementer): the
    planner's own worked example for a backward-clock edge case correctly
    computed the CURRENT call's return value (no refill, return False) but
    then instructed unconditionally overwriting the tracked clock/high-water
    mark with the backward value - which corrupts a LATER call's elapsed-time
    computation (a rate-limit-bypass bug). The implementer followed this
    worked example exactly and failed the hidden acceptance oracle's
    multi-call high-water-mark test as a direct result. _PLANNER_SYSTEM must
    instruct the planner to trace a follow-up call, not just the edge case's
    own immediate return value, whenever the edge case touches state that
    persists across calls."""
    for prompt in (p._PLANNER_SYSTEM, p._REWORK_PLANNER_SYSTEM):
        assert "persist" in prompt.lower()
        assert "follow-up" in prompt.lower() or "subsequent" in prompt.lower()


def test_run_planner_include_scratchpad_augments_system_prompt(agents_dir, monkeypatch):
    """When include_scratchpad=True, the planner's system prompt must direct it
    to weave .agent_scratchpad.md updates into the GENERATED checklist as
    first-class steps - not left to a trailing aside the executor ignores.
    Root cause (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16): across 22 guided
    runs the scratchpad was consumed only twice (9%) because the checklist the
    model actually follows never mentioned it. The clause must reach the
    backend's system arg."""
    fake = _FakePlannerBackend(response="1. Step one.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b", include_scratchpad=True,
    )

    system = fake.calls[0]["system"]
    # The base steering must still be present ...
    assert "implementation file" in system
    # ... plus the scratchpad clause, naming the file and asking for it as a
    # per-step checklist item rather than an afterthought.
    assert ".agent_scratchpad.md" in system
    assert system != p._PLANNER_SYSTEM


def test_run_planner_omits_scratchpad_by_default(agents_dir, monkeypatch):
    """include_scratchpad defaults to False (the H3 ablation "off" arm and any
    caller that doesn't opt in): the system prompt must be exactly
    _PLANNER_SYSTEM, unchanged, so the existing by-reference assertions and the
    scratchpad-off behavior both hold."""
    fake = _FakePlannerBackend(response="1. Step one.")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_planner(
        "Add a rate limiter.", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert fake.calls[0]["system"] == p._PLANNER_SYSTEM
    assert ".agent_scratchpad.md" not in fake.calls[0]["system"]


# ---------- planner independently routable (always-on; see test_always_on_planner.py) ----------
def test_resolve_planner_backend_local_mode_honors_env_var_independent_of_dispatch(
    monkeypatch,
):
    """PIPELINE_BACKEND_PLANNER must route the planner to a provider
    independent of whatever dispatch_backend is - this is the gap fix:
    the planner no longer mirrors dispatch_backend."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "mlx")
    backend_name, _model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
    )
    assert backend_name == "mlx"


def test_resolve_planner_backend_local_mode_honors_plan_role_config(monkeypatch):
    """plan_role_config's model value is a friendly registry key (like the
    registry's own roles.* entries), validated/resolved against
    model_registry.json's real "mlx" provider - "qwen" is the repo-root
    registry's declared mlx model."""
    monkeypatch.delenv("PIPELINE_BACKEND_PLANNER", raising=False)
    backend_name, model = p._resolve_planner_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"planner": {"provider": "mlx", "model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


# ---------- test_author role resolution (TDD_SPLIT_PRODUCTION_PLAN.md §2.2) ----------
def test_resolve_test_author_backend_unconfigured_returns_none_none(monkeypatch):
    """Unlike the planner, an unconfigured test_author role must NOT mirror
    dispatch_backend/local_model - that would reproduce the experiment's
    harmful same-model variant A. (None, None) is the explicit "skip the
    split" signal callers must fail open on. Registry mocked to {} so this
    genuinely tests the unconfigured case regardless of model_registry.json's
    real on-disk contents (which now configures test_author=ollama/glm in
    production, per the validated stronger-author-split experiment)."""
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: {})
    result = p._resolve_test_author_backend("ollama", "gpt-oss:20b")
    assert result == (None, None)


def test_resolve_test_author_backend_honors_env_var(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    backend_name, model = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


def test_resolve_test_author_backend_honors_plan_role_config(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    backend_name, model = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"provider": "mlx", "model": "qwen"}},
    )
    assert backend_name == "mlx"
    assert model == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


def test_resolve_test_author_backend_refuses_same_model_as_dispatch(monkeypatch):
    """Belt-and-suspenders (§2.2): even when a provider IS configured, if it
    resolves to the exact same backend+model dispatch is already using,
    refuse - comparing RESOLVED values catches an operator accidentally
    pointing the test-author at the same concrete model dispatch uses
    (e.g. same Ollama endpoint/tag via a different env var)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "ollama")
    result = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "gpt-oss"}},
    )
    assert result == (None, None)


def test_resolve_test_author_backend_fails_open_on_malformed_registry_model(monkeypatch):
    """A typo'd model name for test_author must degrade to "no split", not
    crash dispatch_story - this role is a bonus, never a gate (§2.5)."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    result = p._resolve_test_author_backend(
        "ollama", "gpt-oss:20b",
        plan_role_config={"test_author": {"model": "no-such-model"}},
    )
    assert result == (None, None)


# ---------- _wait_for_agent_exit (blocking poll used only by the test-author
# phase - unlike the main executor dispatch, this must finish before the
# executor starts, since the executor's prompt/worktree depend on it) ----------
def test_wait_for_agent_exit_returns_true_when_already_reaped():
    """A process that already exited (and was reaped) before the poll loop
    even starts must be treated as "exited", not hang for the full timeout.
    os.waitpid on an already-reaped pid raises ChildProcessError - that's
    the signal, not a bug to guard against."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    result = p._wait_for_agent_exit(proc.pid, timeout=5, poll_interval=0.02)
    assert result is True


def test_wait_for_agent_exit_returns_false_and_kills_on_timeout():
    proc = subprocess.Popen(["sleep", "5"])
    start = time.monotonic()
    result = p._wait_for_agent_exit(proc.pid, timeout=0.3, poll_interval=0.05)
    elapsed = time.monotonic() - start
    try:
        assert result is False
        assert elapsed < 2, "must not block for the full unkilled duration"
    finally:
        try:
            proc.wait(timeout=2)
        except (ChildProcessError, subprocess.TimeoutExpired):
            pass


# ---------- _run_test_author_phase (TDD_SPLIT_PRODUCTION_PLAN.md §2.1/§2.5) ----------
class _FakeTestAuthorBackend:
    def __init__(self, pid, raises=None):
        self.pid = pid
        self.raises = raises
        self.calls = []

    def dispatch(self, prompt, *, system, model, allowed_tools, cwd, log_path, append):
        self.calls.append({
            "prompt": prompt, "system": system, "model": model,
            "allowed_tools": allowed_tools, "cwd": cwd, "log_path": log_path,
            "append": append,
        })
        if self.raises:
            raise self.raises
        return backend.AgentHandle(pid=self.pid)


def _already_reaped_pid():
    """A pid that is guaranteed dead and already reaped, for tests that
    don't care about real dispatch timing - _wait_for_agent_exit treats
    this identically to "the agent exited"."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _make_worktree_repo(tmp_path, branch):
    """Real git repo + worktree on `branch` off base branch "main", no
    commits yet on `branch` beyond the shared base - lets tests exercise
    the real _worktree_has_new_commits check without mocking git."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   capture_output=True, text=True, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(wt)],
                   cwd=repo, capture_output=True, text=True, check=True)
    return repo, wt


def test_test_author_prompt_instructs_committing_the_test_file():
    """Live validation run (2026-07-18, disposable sandbox repo): Claude
    Sonnet wrote a genuinely correct 35-test suite, confirmed it RED, and
    said 'Done' - but never ran `git commit`, because this prompt never
    told it to. _worktree_has_new_commits then saw zero commits and
    _run_test_author_phase reported failure even though the test-authoring
    itself succeeded, leaving an uncommitted test file in the worktree that
    then confused the executor into a repetition-guard park. The executor's
    own prompt (_build_dispatch_command) already ends with "commit your
    work, push the branch, and exit" - the test-author prompt needs the
    equivalent instruction."""
    prompt = p._test_author_prompt("Build a widget.")
    assert "commit" in prompt.lower()


def test_run_test_author_phase_skips_when_role_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_BACKEND_TEST_AUTHOR", raising=False)
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("dispatch must not be reached when unconfigured")

    monkeypatch.setattr(backend, "get_backend", _boom)
    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert result is False


def test_run_test_author_phase_returns_true_on_successful_commit(monkeypatch, tmp_path):
    """When the resolved role differs from dispatch, the dispatch succeeds,
    and the agent branch has a new commit by the time it exits, the phase
    reports success - the commit is what the executor will build on."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")
    (wt / "test_foo.py").write_text("def test_x(): assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "tests"], cwd=wt,
                   capture_output=True, text=True, check=True)

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is True
    assert fake.calls[0]["cwd"] == wt
    assert "Build it." in fake.calls[0]["prompt"]


def test_run_test_author_phase_returns_false_when_no_new_commit(monkeypatch, tmp_path):
    """The agent exited cleanly but never committed anything (e.g. it wrote
    no test file, or wrote one but didn't commit) - the executor has
    nothing to build on, so this must fail open exactly like a timeout."""
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    _repo, wt = _make_worktree_repo(tmp_path, "agent/s1")

    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=wt, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_test_author_phase_returns_false_when_dispatch_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=0, raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_test_author_phase_returns_false_on_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_BACKEND_TEST_AUTHOR", "mlx")
    fake = _FakeTestAuthorBackend(pid=_already_reaped_pid())
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)
    monkeypatch.setattr(p, "_wait_for_agent_exit", lambda *a, **k: False)
    monkeypatch.setattr(pplanner, "_notify_user", lambda *a, **k: None)

    def _boom(*a, **k):
        raise AssertionError("must not check for commits when the dispatch timed out")

    monkeypatch.setattr(p, "_worktree_has_new_commits", _boom)

    result = p._run_test_author_phase(
        {"agent_instructions": "Build it."}, story_key="S1",
        worktree_path=tmp_path, dispatch_backend="ollama", local_model="gpt-oss:20b",
        plan_name="plan",
        plan_role_config={"test_author": {"model": "qwen"}},
    )
    assert result is False


def test_run_rework_planner_routes_to_claude_and_passes_feedback_as_prompt(
    agents_dir, monkeypatch,
):
    """The rework planner translates review feedback into a fix checklist -
    same bounded-call/backend-resolution machinery as _run_planner, but a
    distinct system prompt and the review feedback (not agent_instructions)
    as the input."""
    monkeypatch.setenv("PIPELINE_BACKEND_PLANNER", "claude")
    fake = _FakePlannerBackend(response="1. Fix the double-count bug\n2. Add a regression test")
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_rework_planner(
        "allow() double-counts refill on every call.",
        dispatch_backend="local", local_model="gpt-oss:20b",
    )

    assert result == "1. Fix the double-count bug\n2. Add a regression test"
    assert calls == [{"role": "planner", "name": "claude"}]
    assert fake.calls[0]["prompt"] == "allow() double-counts refill on every call."
    assert fake.calls[0]["system"] == p._REWORK_PLANNER_SYSTEM
    assert fake.calls[0]["system"] != p._PLANNER_SYSTEM

def test_rework_planner_exception_for_small_edits():
    """The rework planner should allow str_replace for small targeted edits."""
    assert "str_replace" in p._REWORK_PLANNER_SYSTEM
    assert "preserve" in p._REWORK_PLANNER_SYSTEM
    assert p._REWORK_PLANNER_SYSTEM != p._PLANNER_SYSTEM


def test_rework_planner_system_prescribes_delegate_wrapper_for_large_function_edits():
    """Mirrors test_planner_system_prescribes_delegate_wrapper_for_large_function_edits
    - a rework cycle's fix checklist needs the same edit-shape guidance as
    the initial checklist, since a review's requested fix can land inside
    the same kind of large existing function."""
    assert "_impl" in p._REWORK_PLANNER_SYSTEM
    assert "delegate" in p._REWORK_PLANNER_SYSTEM.lower()


def test_rework_planner_system_delegate_wrapper_specifies_what_to_preserve():
    """Mirrors test_planner_system_delegate_wrapper_specifies_what_to_preserve
    - a rework cycle's fix checklist needs the same completed rename-and-
    delegate recipe as the initial checklist, since a reviewer's requested
    fix can land inside the same large-function-wrap shape (this is exactly
    where it recurred live: the story's own rework cycle re-applied the
    same incomplete recipe and reproduced the same two Blocking findings)."""
    text = p._REWORK_PLANNER_SYSTEM.lower()
    assert "decorator" in text
    assert "docstring" in text
    assert "valid" in text and "before" in text


def test_run_rework_planner_returns_none_on_backend_failure(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    result = p._run_rework_planner(
        "bug description", dispatch_backend="local",
        local_model="gpt-oss:20b",
    )

    assert result is None


# ---------- decompose role (provider-configurable product-analyst) ----------
def test_extract_json_block_strips_json_fence():
    text = '```json\n{"epics": []}\n```'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_extract_json_block_strips_bare_fence_without_json_tag():
    text = '```\n{"epics": []}\n```'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_extract_json_block_returns_text_unchanged_when_no_fence():
    text = '{"epics": []}'
    assert p._extract_json_block(text) == '{"epics": []}'


def test_run_decompose_calls_registry_resolved_backend_with_product_analyst_persona(
    agents_dir, monkeypatch,
):
    """_run_decompose routes through role_registry.resolve_role("decompose"),
    so against the REAL registry it resolves to the stock roles.decompose
    entry - ollama/glm (tag glm-5.2:cloud) while Claude usage is capped - and
    seeds the product-analyst persona body. (The persona's own declared
    model is only used as the fallback when the registry has NO
    roles.decompose entry; the registry entry wins now that one exists.)"""
    fake = _FakePlannerBackend(response='{"epics": []}')
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    result = p._run_decompose("Build a CLI todo app.")

    assert result == '{"epics": []}'
    assert calls == [{"role": "decompose", "name": "ollama"}]
    assert fake.calls[0]["prompt"] == "Build a CLI todo app."
    assert "Analyst body." in fake.calls[0]["system"]
    # registry roles.decompose.model is glm (resolved to its tag).
    assert fake.calls[0]["model"] == "glm-5.2:cloud"


def test_run_decompose_routes_to_registry_configured_provider(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}},
        "roles": {"decompose": {"provider": "ollama", "model": "gpt-oss"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    calls = []

    def _fake_get_backend(role, *, name=None):
        calls.append({"role": role, "name": name})
        return fake

    monkeypatch.setattr(backend, "get_backend", _fake_get_backend)

    p._run_decompose("Build a CLI todo app.")

    assert calls == [{"role": "decompose", "name": "ollama"}]
    assert fake.calls[0]["model"] == "gpt-oss:20b"


def test_run_decompose_appends_claude_tier_guidance_by_default(agents_dir, monkeypatch):
    """No dispatch override configured -> resolves to the "claude" tier and
    that guidance (not the local/cloud-oss variants) is appended after the
    persona body."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {"providers": {}, "roles": {}}
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    system = fake.calls[0]["system"]
    assert "Analyst body." in system
    assert "Target implementer: Claude-class" in system
    assert "local ~20B-class" not in system


def test_run_decompose_appends_local_tier_guidance_for_local_dispatch(
    agents_dir, monkeypatch,
):
    """A dispatch role resolved to a non-claude provider with a plain
    (non-":cloud") model tag is treated as a local ~20B-class implementer,
    and the corresponding splitting guidance is appended."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}},
        "roles": {"dispatch": {"provider": "ollama", "model": "gpt-oss"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    assert "Target implementer: local ~20B-class" in fake.calls[0]["system"]


def test_run_decompose_appends_cloud_oss_tier_guidance_for_cloud_tagged_dispatch(
    agents_dir, monkeypatch,
):
    """A dispatch role resolved to a ":cloud"-suffixed tag (e.g. glm served
    through ollama) is a cloud open-source implementer, not local - provider
    name alone can't tell these apart."""
    fake = _FakePlannerBackend(response='{"epics": []}')
    registry = {
        "providers": {"ollama": {"models": {"glm": {"tag": "glm-5.2:cloud"}}}},
        "roles": {"dispatch": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    p._run_decompose("Build a CLI todo app.")

    system = fake.calls[0]["system"]
    assert "Target implementer: cloud open-source" in system
    assert "local ~20B-class" not in system


def test_dispatch_strength_tier_fails_open_to_claude_on_registry_error(monkeypatch):
    def _raise(*a, **k):
        raise role_registry.RoleRegistryError("bad registry")

    monkeypatch.setattr(role_registry, "resolve_role", _raise)

    assert p._dispatch_strength_tier() == "claude"


def test_run_decompose_returns_none_on_backend_failure(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(raises=RuntimeError("endpoint unreachable"))
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    assert p._run_decompose("Build a CLI todo app.") is None


def test_run_decompose_returns_none_on_empty_response(agents_dir, monkeypatch):
    fake = _FakePlannerBackend(response="   \n  ")
    monkeypatch.setattr(backend, "get_backend", lambda role, *, name=None: fake)

    assert p._run_decompose("Build a CLI todo app.") is None


def test_decompose_plan_happy_path_parses_fenced_json(agents_dir, monkeypatch):
    plan_json = json.dumps({"epics": [{"summary": "E1", "stories": []}]})
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: f"```json\n{plan_json}\n```")

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True
    assert result["plan"]["epics"][0]["summary"] == "E1"


def test_decompose_plan_malformed_json_fails_with_raw_text_preserved(
    agents_dir, monkeypatch,
):
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: "not json at all")

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "raw" in result
    assert result["raw"] == "not json at all"


def test_decompose_plan_rejects_json_missing_epics_list(agents_dir, monkeypatch):
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: '{"not_epics": []}')

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False
    assert "epics" in result["error"]


def test_decompose_plan_fails_open_when_backend_returns_none(agents_dir, monkeypatch):
    """_run_decompose already fails open (returns None) on a broken backend -
    decompose_plan must surface that as ok=False, never raise."""
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: None)

    result = p.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is False


def test_dispatch_story_decompose_rework_translates_feedback_into_fix_checklist(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch that resumes via transcript (always-on for
    local-family dispatch) must translate the raw review feedback into a
    tech-lead fix checklist (via _run_rework_planner) before appending it -
    not pasted verbatim, mirroring the initial-dispatch checklist treatment."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    transcript_path = worktree_path / ".agent_transcript.json"
    transcript_path.write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "dcrework", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "allow() double-counts refill on every call."},
    })

    rework_calls = []

    def _fake_rework_planner(review_feedback, *, dispatch_backend, local_model, **k):
        rework_calls.append({
            "review_feedback": review_feedback,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
        })
        return "1. Reproduce the double-count with a test.\n2. Fix allow()."

    monkeypatch.setattr(p, "_run_rework_planner", _fake_rework_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9010)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcrework", "S1")

    assert result["ok"] is True
    assert len(rework_calls) == 1
    assert rework_calls[0]["review_feedback"] == "allow() double-counts refill on every call."

    append_content = popen_calls[0]["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "1. Reproduce the double-count with a test." in append_content
    # The original raw feedback stays present for reference, not replaced.
    assert "allow() double-counts refill on every call." in append_content


# Removed: test_dispatch_story_decompose_off_rework_uses_raw_feedback_unchanged
# asserted the now-removed "PIPELINE_DECOMPOSE=off skips the rework planner"
# behavior. Under always-on the rework planner runs for local-family dispatch;
# the raw-feedback fallback now lives only in the fail-open case, covered by
# test_dispatch_story_decompose_rework_fails_open_when_fix_planner_returns_none.


def test_dispatch_story_decompose_rework_fails_open_when_fix_planner_returns_none(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A failed rework-planner call must fall back to the raw-feedback
    format, never block or corrupt the rework redispatch."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    _write_manifest(plan_dir, "dcreworknone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "changes_requested", "worktree": str(worktree_path),
               "review_feedback": "The SQL is injectable; parameterize it."},
    })

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9012)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcreworknone", "S1")

    assert result["ok"] is True
    assert popen_calls[0]["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        "Address this feedback:\nThe SQL is injectable; parameterize it."
    )


# Removed: test_dispatch_story_decompose_off_by_default_skips_planner asserted
# the now-removed "PIPELINE_DECOMPOSE unset = planner never invoked" behavior.
# Under always-on the planner runs for local-family dispatch; the positive
# case is covered by test_dispatch_story_planner_always_runs_for_local_when_
# decompose_unset in test_always_on_planner.py, and the claude-backend skip by
# test_dispatch_story_claude_backend_skips_planner there.


def test_dispatch_story_writes_plan_and_augments_local_prompt(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A local-family dispatch (always-on planner) must run the planner, write
    the resulting checklist to .agent_plan.md, and augment the executor's
    task prompt with it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dccloud", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)

    planner_calls = []

    def _fake_planner(agent_instructions, *, dispatch_backend, local_model,
                      **kwargs):
        planner_calls.append({
            "agent_instructions": agent_instructions,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
        })
        return "1. Write a failing test for the limiter.\n2. Implement it."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9002)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dccloud", "S1")

    assert result["ok"] is True
    assert len(planner_calls) == 1
    assert planner_calls[0]["dispatch_backend"] == "local"
    assert planner_calls[0]["agent_instructions"] == "Build it."

    plan_path = worktree_root / "S1" / ".agent_plan.md"
    assert plan_path.exists()
    assert plan_path.read_text() == "1. Write a failing test for the limiter.\n2. Implement it."

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert "1. Write a failing test for the limiter." in task
    assert ".agent_scratchpad.md" in task


def test_dispatch_story_decompose_scratchpad_off_omits_scratchpad_instruction(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD=off is the H3 ablation arm (checklist
    only, no persistent scratchpad) - the checklist must still reach the
    executor, but the scratchpad instruction must not."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", "off")
    _write_manifest(plan_dir, "dcnoscratch", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner",
                        lambda *a, **k: "1. Write a failing test.\n2. Implement it.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9007)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcnoscratch", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert "1. Write a failing test." in task
    assert ".agent_scratchpad.md" not in task


def test_dispatch_story_decompose_scratchpad_defaults_on(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """PIPELINE_DECOMPOSE_SCRATCHPAD unset must default to "on" - the
    scratchpad instruction ships by default whenever decompose is enabled."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcscratchdefault", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    # TDD-split is unconditional for local-family dispatch now; neutralize it
    # here so this test's popen_calls captures only the main executor's
    # dispatch, not an incidental test-author sub-dispatch.
    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: "1. Step one.")
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9008)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcscratchdefault", "S1")

    assert result["ok"] is True
    assert ".agent_scratchpad.md" in popen_calls[0]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_decompose_passes_include_scratchpad_flag_to_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The scratchpad must be a first-class step IN the generated checklist,
    not just a trailing aside on the prompt. So when the scratchpad is enabled,
    dispatch must call _run_planner with include_scratchpad=True (so the
    planner weaves it into the steps); when disabled (H3 ablation), with
    include_scratchpad=False. Regression guard for the 9%-consumption root
    cause (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")

    def _run_dispatch(plan_name, story_key, scratchpad_env):
        if scratchpad_env is None:
            monkeypatch.delenv("PIPELINE_DECOMPOSE_SCRATCHPAD", raising=False)
        else:
            monkeypatch.setenv("PIPELINE_DECOMPOSE_SCRATCHPAD", scratchpad_env)
        # Distinct story_key per sub-dispatch: the worktree (and its
        # .agent_plan.md) is keyed by story_key, so reusing one would let the
        # second dispatch find the first's plan and skip _run_planner.
        _write_manifest(plan_dir, plan_name, {
            story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                        "status": "todo", "dependencies": []},
        })
        planner_kwargs = {}

        def _fake_planner(agent_instructions, **kwargs):
            planner_kwargs.update(kwargs)
            return "1. Step one."

        monkeypatch.setattr(p, "_run_planner", _fake_planner)
        monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
        monkeypatch.setattr(backend.subprocess, "Popen",
                            lambda cmd, env, **kw: _FakeProc(9009))
        monkeypatch.setattr(pt, "plane_request",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
        monkeypatch.setattr(p, "_default_branch", lambda: "main")
        assert p.dispatch_story(plan_name, story_key)["ok"] is True
        return planner_kwargs

    # Default (unset) -> on -> planner told to include the scratchpad step.
    assert _run_dispatch("dcincl_default", "SDEF", None)["include_scratchpad"] is True
    # Explicit on -> same.
    assert _run_dispatch("dcincl_on", "SON", "on")["include_scratchpad"] is True
    # H3 ablation off -> planner must NOT weave it in.
    assert _run_dispatch("dcincl_off", "SOFF", "off")["include_scratchpad"] is False


def test_dispatch_story_passes_plan_role_config_from_manifest_to_planner(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """End-to-end: a plan's manifest role_config block must actually reach
    _run_planner's plan_role_config kwarg via dispatch_story - not just be
    tolerated by signature."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    (plan_dir / "dpcfg.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "SPC": {"summary": "Do thing", "agent_instructions": "Build it.",
                     "status": "todo", "dependencies": []},
        },
        "repo_root": str(plan_dir),
        "role_config": {"planner": {"provider": "mlx"}},
    }))
    captured = {}

    def _fake_planner(agent_instructions, **kwargs):
        captured.update(kwargs)
        return "1. Step one."

    monkeypatch.setattr(p, "_run_planner", _fake_planner)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, env, **kw: _FakeProc(9009))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    assert p.dispatch_story("dpcfg", "SPC")["ok"] is True

    assert captured["plan_role_config"] == {"planner": {"provider": "mlx"}}


def test_dispatch_story_decompose_skips_for_claude_backend(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The checklist crutch exists for the weak local executor only - a
    Claude dispatch must never trigger planning even with decompose enabled."""
    _write_manifest(plan_dir, "dcclaude", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    def _boom(*a, **k):
        raise AssertionError("planner must not run for a Claude dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9003))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcclaude", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".agent_plan.md").exists()


def test_dispatch_story_decompose_fails_open_when_planner_returns_none(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A planner that fails (returns None) must not block or alter dispatch -
    the story proceeds exactly like PIPELINE_DECOMPOSE=off."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcnone", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    monkeypatch.setattr(p, "_run_planner", lambda *a, **k: None)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9004)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcnone", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".agent_plan.md").exists()
    # test_author now resolves to claude/sonnet (model_registry.json), so it
    # issues its own leading Popen call (claude backend env, no
    # LOCAL_AGENT_TASK) before the local executor's - assert against the
    # last call, which is the executor's.
    assert ".agent_scratchpad.md" not in popen_calls[-1]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_decompose_skips_replanning_on_resume_but_keeps_referencing_plan(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A resumed (interrupted) local story whose worktree already carries a
    plan from its first dispatch must NOT call the planner again (spending a
    second LLM call) but the rebuilt resume prompt must still reference the
    existing checklist, so the executor doesn't lose the guidance on resume."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir()
    (worktree_path / ".agent_plan.md").write_text("1. Existing checklist step.")
    # The checklist-reuse guard (server.py:~1899) requires a .agent_plan_src_hash
    # whose content matches a sha256 of the story's agent_instructions, else a
    # resumed dispatch silently drops the checklist from the rebuilt prompt.
    (worktree_path / ".agent_plan_src_hash").write_text(
        hashlib.sha256(b"Build it.").hexdigest()
    )
    _write_manifest(plan_dir, "dcresume", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "worktree": str(worktree_path),
               "last_commit": "sha-1"},
    })
    (plan_dir / "dcresume.S1.journal.json").write_text(json.dumps([
        {"step": "step-1", "summary": "Wrote the parser",
         "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
    ]))

    def _boom(*a, **k):
        raise AssertionError("planner must not re-run on a resumed dispatch")

    monkeypatch.setattr(p, "_run_planner", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9005)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("dcresume", "S1")

    assert result["ok"] is True
    assert result["resumed"] is True
    # Plan file untouched - not overwritten by a (forbidden) re-plan call.
    assert (worktree_path / ".agent_plan.md").read_text() == "1. Existing checklist step."
    assert "1. Existing checklist step." in popen_calls[0]["env"]["LOCAL_AGENT_TASK"]


def test_dispatch_story_excludes_decompose_artifacts_from_worktree_tracking(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Mirrors test_dispatch_story_excludes_review_and_agent_log_from_worktree_tracking
    (Mode 17) for the two new guided-decomposition artifacts: an untracked
    .agent_plan.md/.agent_scratchpad.md that later gets swept into a rework
    WIP-commit's `git add -A` would dirty the tree ahead of the pre-merge
    rebase the same way review.log did."""
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=real_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=real_repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                    cwd=real_repo, check=True)
    bare_origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare_origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_origin)], cwd=real_repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=real_repo, check=True)

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "dcexcl", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })
    manifest_path = plan_dir / "dcexcl.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(p, "_run_planner",
                        lambda *a, **k: "1. Checklist step.")
    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and str(cmd[0]).endswith("python3"):
            return _FakeProc(9006)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    result = p.dispatch_story("dcexcl", "S1")
    assert result["ok"] is True

    exclude_content = (real_repo / ".git" / "info" / "exclude").read_text()
    assert ".agent_plan.md" in exclude_content
    assert ".agent_scratchpad.md" in exclude_content

    worktree_path = worktree_root / "S1"
    assert (worktree_path / ".agent_plan.md").exists()
    (worktree_path / ".agent_scratchpad.md").write_text("step 1 done\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=worktree_path, capture_output=True, text=True, check=True)
    assert ".agent_plan.md" not in staged.stdout
    assert ".agent_scratchpad.md" not in staged.stdout


# ---------- dispatch_story wiring for the TDD-split test-author phase
# (TDD_SPLIT_PRODUCTION_PLAN.md §2.1/§2.4) ----------
def test_dispatch_story_tdd_split_unset_opted_in_runs_test_author_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """With the global PIPELINE_TDD_SPLIT toggle removed, an opted-in
    (tdd_split=true) non-resuming story RUNS the test-author phase even
    with the env var unset - the per-story tdd_split field is the sole
    opt-in (Secure Defaults: opt-in is per story, not per env)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdoff", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })

    def _run(*a, **k):
        nonlocal ran
        ran = True
        return True

    ran = False
    monkeypatch.setattr(p, "_run_test_author_phase", _run)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9101))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdoff", "S1")

    assert result["ok"] is True
    assert ran is True
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()

def test_dispatch_story_tdd_split_opted_in_runs_phase_and_augments_prompt(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An opted-in (tdd_split=true) non-resuming story runs the test-author
    phase and the executor prompt is augmented with the NEVER-touch-tests
    steering (the global PIPELINE_TDD_SPLIT toggle is gone; opt-in is solely
    the per-story field)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdon", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })

    phase_calls = []

    def _fake_phase(story, *, story_key, worktree_path, dispatch_backend,
                    local_model, plan_role_config=None, **kwargs):
        phase_calls.append({
            "story_key": story_key, "worktree_path": worktree_path,
            "dispatch_backend": dispatch_backend, "local_model": local_model,
            "plan_role_config": plan_role_config,
        })
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _fake_phase)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9103)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdon", "S1")

    assert result["ok"] is True
    assert len(phase_calls) == 1
    assert phase_calls[0]["story_key"] == "S1"
    assert phase_calls[0]["worktree_path"] == worktree_root / "S1"
    assert phase_calls[0]["dispatch_backend"] == "local"

    marker = worktree_root / "S1" / ".tdd_split_test_author_done"
    assert marker.exists()

    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


def test_dispatch_story_tdd_split_phase_failure_falls_open_to_monolithic(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A test-author phase that fails (returns False - unresolved role,
    dispatch error, timeout, or no commit) must leave no marker and must
    NOT augment the executor prompt - the story proceeds exactly like a
    story with no split configured (§2.5's fail-open contract)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdfail", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": [], "tdd_split": True},
    })

    monkeypatch.setattr(p, "_run_test_author_phase", lambda *a, **k: False)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9104)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdfail", "S1")

    assert result["ok"] is True
    assert not (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING not in task


def test_dispatch_story_tdd_split_skips_rerun_when_marker_already_present(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A rework redispatch on a worktree that already has a test-author
    commit must not run the phase again (§2.1: reworks act on the SAME
    tests) - but the executor prompt must still reference them."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdmarker", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "interrupted", "dependencies": [], "tdd_split": True},
    })
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    (worktree_path / ".tdd_split_test_author_done").write_text("ok\n")

    def _boom(*a, **k):
        raise AssertionError("must not re-run the test-author phase on a resumed worktree")

    monkeypatch.setattr(p, "_run_test_author_phase", _boom)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(9105)

    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdmarker", "S1")

    assert result["ok"] is True
    task = popen_calls[0]["env"]["LOCAL_AGENT_TASK"]
    assert p._NEVER_TOUCH_TESTS_STEERING in task


def test_dispatch_story_tdd_split_story_without_opt_in_field_still_runs_phase(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The per-story `tdd_split` opt-in field has been removed as a gate
    (see PLAN_RETROSPECTIVE_PROCESS_PLAN.md / retros/tdd-split-always-on):
    the split is now unconditional for local-family dispatch, mirroring the
    guided-decomposition planner. A story with no `tdd_split` key at all
    still runs the test-author phase - there is no per-story escape hatch
    anymore, matching the operator's directive that removing the toggle
    meant "always on for every story", not "opt-in per story"."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tdnoopt", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    })

    ran = False

    def _run(*a, **k):
        nonlocal ran
        ran = True
        return True

    monkeypatch.setattr(p, "_run_test_author_phase", _run)
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda cmd, **kw: _FakeProc(9102))
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p.dispatch_story("tdnoopt", "S1")

    assert result["ok"] is True
    assert ran is True
    assert (worktree_root / "S1" / ".tdd_split_test_author_done").exists()
