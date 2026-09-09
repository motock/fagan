"""Path-variable entries in pipeline.config_provenance's ENV_VAR_CATALOG.

Story: make the path variables (PLAN_DIR, WORKTREE_ROOT, AGENTS_DIR,
OVERLORD_POLICY, USAGE_STATE_PATH) visible in the effective-config view.

ENV_VAR_CATALOG is a CUMULATIVE registry that this story grows and later
stories will grow further, so every assertion here is MEMBERSHIP-based:
we assert the five names this story adds (and that a few pre-existing
anchor entries survive unchanged) - never the catalog's exact contents,
length, or order.

The catalogued defaults must match pipeline/paths.py EXACTLY. Rather than
trusting a transcription, the tests parse pipeline/paths.py's source with
``ast`` and cross-check each default literal against the corresponding
``os.environ.get(NAME, DEFAULT)`` call there (paths.py is NOT imported:
importing it would expanduser() against the real environment).
"""
import ast
import dataclasses
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The five (env var name -> default literal) pairs this story adds to
# ENV_VAR_CATALOG. Defaults must match pipeline/paths.py exactly.
PATH_VAR_DEFAULTS = {
    "PLAN_DIR": "~/.claude/plans",
    "WORKTREE_ROOT": "~/.claude/worktrees",
    "AGENTS_DIR": "~/.claude/agents",
    "OVERLORD_POLICY": "~/.claude/overlord-policy.md",
    "USAGE_STATE_PATH": "~/.claude/usage_state.json",
}

# Pre-existing catalog anchors that must survive this story untouched
# (membership + default only - the catalog stays free to grow).
PRE_EXISTING_ANCHORS = {
    "PIPELINE_AUTONOMY": "gated",
    "PIPELINE_LOCAL_MAX_STEPS": "40",
    "PIPELINE_AUTO_ESCALATE": None,
    "PIPELINE_ESCALATION_MODEL": None,
    "PIPELINE_WEDGE_SCAN_ENABLED": "1",
}


def _import_module():
    import pipeline.config_provenance as mod

    return mod


def _catalog_by_name(mod):
    return {spec.name: spec for spec in mod.ENV_VAR_CATALOG}


def _paths_py_env_defaults():
    """Map env-var name -> default literal for every
    ``os.environ.get("NAME", "DEFAULT")`` call in pipeline/paths.py."""
    source = (REPO_ROOT / "pipeline" / "paths.py").read_text(encoding="utf-8")
    defaults = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        recv = func.value
        if not (
            isinstance(recv, ast.Attribute)
            and recv.attr == "environ"
            and isinstance(recv.value, ast.Name)
            and recv.value.id == "os"
        ):
            continue
        if len(node.args) >= 2 and all(
            isinstance(a, ast.Constant) for a in node.args[:2]
        ):
            defaults[node.args[0].value] = node.args[1].value
    return defaults


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_path_var_is_catalog_member(name):
    mod = _import_module()
    catalog = _catalog_by_name(mod)
    assert name in catalog, (
        f"ENV_VAR_CATALOG is missing {name!r}; catalogued names: {sorted(catalog)}"
    )


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_path_var_catalog_entry_shape_and_default(name):
    mod = _import_module()
    spec = _catalog_by_name(mod)[name]
    assert isinstance(spec, mod.EnvVarSpec)
    # EnvVarSpec's field shape is pinned elsewhere too; re-assert here so this
    # file alone catches an implementer inventing fields while adding entries.
    assert {f.name for f in dataclasses.fields(mod.EnvVarSpec)} == {"name", "default"}
    assert isinstance(spec.default, str), (
        f"{name} must catalogue a string default, got {spec.default!r}"
    )
    assert spec.default == PATH_VAR_DEFAULTS[name]


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_path_var_default_matches_paths_py_literal(name):
    paths_defaults = _paths_py_env_defaults()
    assert name in paths_defaults, (
        f"pipeline/paths.py no longer reads {name!r} via os.environ.get"
    )
    assert paths_defaults[name] == PATH_VAR_DEFAULTS[name], (
        f"pipeline/paths.py default for {name} drifted from the catalogued literal"
    )


@pytest.mark.parametrize(
    "name, expected_default", sorted(PRE_EXISTING_ANCHORS.items())
)
def test_pre_existing_catalog_entries_unchanged(name, expected_default):
    mod = _import_module()
    catalog = _catalog_by_name(mod)
    assert name in catalog, f"pre-existing catalog entry {name!r} was removed"
    assert catalog[name].default == expected_default, (
        f"pre-existing default for {name!r} was changed"
    )


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_resolve_path_var_unset_reports_code_default(name):
    mod = _import_module()
    spec = _catalog_by_name(mod)[name]
    result = mod.resolve_env_var(
        name, spec.default, environ={}, plist_env={}, mcp_env={}
    )
    assert result["source"] == "code_default"
    assert result["effective"] == spec.default


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_resolve_path_var_process_env_wins(name):
    mod = _import_module()
    _spec = _catalog_by_name(mod)[name]
    result = mod.resolve_env_var(
        name, _spec.default, environ={name: "/tmp/x"}, plist_env={}, mcp_env={}
    )
    assert result["source"] == "process_env"
    assert result["effective"] == "/tmp/x"


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_path_var_is_not_treated_as_secret(name):
    mod = _import_module()
    assert mod._is_secret(name) is False, (
        f"{name} is a filesystem path, not a credential - _is_secret() must "
        "not mask it"
    )


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_resolve_path_var_not_masked_unset(name):
    mod = _import_module()
    spec = _catalog_by_name(mod)[name]
    result = mod.resolve_env_var(
        name, spec.default, environ={}, plist_env={}, mcp_env={}
    )
    assert result["masked"] is False
    assert result["effective"] != "***"
    assert result["effective"] == spec.default


@pytest.mark.parametrize("name", sorted(PATH_VAR_DEFAULTS))
def test_resolve_path_var_not_masked_set(name):
    mod = _import_module()
    _spec = _catalog_by_name(mod)[name]
    result = mod.resolve_env_var(
        name, _spec.default, environ={name: "/tmp/x"}, plist_env={}, mcp_env={}
    )
    assert result["masked"] is False
    assert result["effective"] == "/tmp/x"
    assert result["effective"] != "***"


def test_effective_env_config_view_includes_path_vars():
    mod = _import_module()
    catalog = _catalog_by_name(mod)
    results = mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
    by_name = {entry["name"]: entry for entry in results}
    for name, expected_default in PATH_VAR_DEFAULTS.items():
        assert name in by_name, (
            f"effective-config view does not show {name!r}"
        )
        assert by_name[name]["effective"] == catalog[name].default
        assert by_name[name]["effective"] == expected_default