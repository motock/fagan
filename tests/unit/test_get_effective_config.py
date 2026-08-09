"""Tests for the get_effective_config MCP tool (pipeline/server.py).

get_effective_config is a purely additive, read-only tool: it aggregates
config_provenance.effective_role_config / effective_env_config /
ignored_env_vars_present into one snapshot, plus a "sources" block naming
which config files were consulted. It must never raise - a misconfigured
role reports a non-None "error" key on its own entry rather than blowing up
the whole call.
"""
import json

import pytest

from app import role_registry
from pipeline import concurrency as pcon
from pipeline import config_provenance
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    (d / "security-engineer.md").write_text(
        '---\nname: "security-engineer"\nmodel: sonnet\n---\n\nSecurity body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def known_registry(monkeypatch):
    """A small, deterministic model_registry.json substitute so tests don't
    depend on the real repo file's current contents."""
    registry = {
        "providers": {
            "claude": {"models": {"opus": {"tag": "opus"}, "sonnet": {"tag": "sonnet"}}},
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}},
        },
        "roles": {
            "dispatch": {"provider": "claude", "model": "sonnet"},
        },
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: registry)
    return registry


# ---------- 1. Registration path ----------


def test_get_effective_config_is_registered_and_wired():
    assert (
        p.mcp._tool_manager._tools["get_effective_config"].fn is p.get_effective_config
    )


# ---------- 2. Happy path: ok, all roles, non-None top-level keys ----------


def test_no_args_returns_ok_with_all_roles_and_top_level_keys(agents_dir, known_registry):
    result = p.get_effective_config()

    assert result["ok"] is True
    role_names = {entry["role"] for entry in result["roles"]}
    assert role_names == set(config_provenance.PIPELINE_ROLES)
    assert result["env"] is not None
    assert result["ignored_env_vars"] is not None
    assert result["sources"] is not None


# ---------- 3. Sources block names all three files with exists booleans ----------


def test_sources_names_all_three_files_with_exists_flag(agents_dir, known_registry):
    result = p.get_effective_config()

    sources = result["sources"]
    for key in ("launchd_plist", "mcp_server_env", "model_registry"):
        assert key in sources, f"sources missing {key!r}: {sources}"
        assert "path" in sources[key]
        assert isinstance(sources[key]["exists"], bool)


# ---------- 4. plan_name layers in role_config overrides ----------


def test_plan_role_config_override_reports_provider_and_source(
    agents_dir, plan_dir, known_registry
):
    (plan_dir / "cfgplan.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"review": {"provider": "mlx", "model": "qwen"}},
    }))

    result = p.get_effective_config(plan_name="cfgplan")

    roles_by_name = {entry["role"]: entry for entry in result["roles"]}
    review = roles_by_name["review"]
    assert review["provider"] == "mlx"
    assert review["provider_source"] == "plan_role_config"


# ---------- 5. Negative: nonexistent plan resolves with no overrides ----------


def test_nonexistent_plan_name_resolves_with_no_overrides(agents_dir, plan_dir, known_registry):
    result = p.get_effective_config(plan_name="does-not-exist")

    assert result["ok"] is True
    roles_by_name = {entry["role"]: entry for entry in result["roles"]}
    for entry in roles_by_name.values():
        assert entry["provider_source"] != "plan_role_config"


# ---------- 6. Fail-open: one role raising RoleRegistryError doesn't blow up the call ----------


def test_fail_open_when_one_role_raises_role_registry_error(
    agents_dir, known_registry, monkeypatch
):
    real_resolve_role = role_registry.resolve_role

    def flaky_resolve_role(role, **kwargs):
        if role == "test_author":
            raise role_registry.RoleRegistryError("simulated failure")
        return real_resolve_role(role, **kwargs)

    monkeypatch.setattr(role_registry, "resolve_role", flaky_resolve_role)

    result = p.get_effective_config()

    assert result["ok"] is True
    roles_by_name = {entry["role"]: entry for entry in result["roles"]}
    assert roles_by_name["test_author"]["error"] is not None
    for name, entry in roles_by_name.items():
        if name == "test_author":
            continue
        assert entry is not None


# ---------- 7. get_role_config is unchanged (regression guard) ----------


def test_get_role_config_still_works_after_addition(agents_dir, known_registry):
    result = p.get_role_config()

    assert result["ok"] is True
    assert set(result["roles"]) == {"overlord", "planner", "dispatch", "review", "decompose"}
