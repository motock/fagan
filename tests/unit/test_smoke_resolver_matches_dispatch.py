"""Parity guard: the smoke's announced dispatch backend must equal real dispatch.

Story SRR-1. ``scripts/smoke_getting_started.py`` used to carry its own
hand-rolled, env-only resolver (``_default_dispatch_resolver``) that read
``PIPELINE_BACKEND_DISPATCH`` raw and deliberately ignored
``model_registry.json``'s ``roles.dispatch`` entry. Since REG-1..REG-5 the
registry IS the source of truth for dispatch - ``app.role_registry.resolve_role``
outranks ``PIPELINE_BACKEND_<ROLE>``, with plan/story overrides above it - so
the smoke's announce line disagreed with the backend real dispatch would use,
and it printed an "advisory" note reassuring the operator that the registry
"only feeds the dashboard display and decompose-time sizing, never real
dispatch". That reassurance is now false, and the announce line exists
precisely so nobody is misled about which backend was validated.

These tests pin the new truth:

1. POSITIVE - a registry ``roles.dispatch`` entry drives the announced backend.
2. PARITY - the announced (provider, model) equals
   ``role_registry.resolve_role("dispatch")`` for the same synthetic inputs.
3. REGISTRY OUTRANKS ENV - the registry wins over ``PIPELINE_BACKEND_DISPATCH``.
4. BOUNDARY - empty state: no registry entry + env set -> env wins; neither
   set -> claude.
5. NEGATIVE - an unrecognised provider still exits 2 naming the value.
6. The output makes no claim that the registry is advisory / does not affect
   dispatch, and the advisory plumbing is gone.

Every registry and environ mapping here is SYNTHETIC. Nothing asserts against
this machine's real ``model_registry.json``: the registry SOURCE
(``app.role_registry.load_registry``) is stubbed and
``PIPELINE_MODEL_REGISTRY_PATH`` points at a temp file with the same contents,
so the real ``resolve_role`` precedence logic runs against synthetic inputs.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Phrases that assert the registry is NOT consulted for dispatch. Every one of
# them is false after REG-1..REG-5 and must not survive anywhere in the file.
FALSE_REGISTRY_CLAIMS = (
    "never real dispatch",
    "only feeds the dashboard",
    "decompose-time sizing",
    "never consults",
    "never gates dispatch",
    "never part of the resolution",
    "does not affect this check",
)

# Names that exist solely to serve the now-obsolete advisory note.
ADVISORY_PLUMBING_NAMES = (
    "_advise_if_registry_differs",
    "_is_registry_dispatch_source",
    "_registry_routed_refusal",
    "_DispatchResolutionError",
)

PRECEDENCE_WORDS = (
    "outrank",
    "beats",
    "precedence",
    "takes priority",
    "wins",
    "authoritative",
    "source of truth",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load_script():
    """Import scripts/smoke_getting_started.py as a module (cached)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    mod_name = "smoke_getting_started_srr1_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _script_text() -> str:
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    return SCRIPT_PATH.read_text()


def _script_tree() -> ast.Module:
    return ast.parse(_script_text())


def _module_docstring() -> str:
    return ast.get_docstring(_script_tree()) or ""


def _function_docstring(name: str) -> str:
    for node in _script_tree().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_docstring(node) or ""
    return ""


def _registry_with_dispatch(provider: str, model: str, tag: str) -> dict:
    """A synthetic registry whose roles.dispatch pins provider/model."""
    return {
        "providers": {provider: {"models": {model: {"tag": tag}}}},
        "roles": {"dispatch": {"provider": provider, "model": model}},
    }


def _registry_provider_only(provider: str) -> dict:
    """A synthetic registry with a provider-only roles.dispatch entry."""
    return {
        "providers": {provider: {"models": {}}},
        "roles": {"dispatch": {"provider": provider}},
    }


def _install(monkeypatch, tmp_path, registry: dict, environ: dict) -> None:
    """Point the smoke's registry + env at SYNTHETIC inputs.

    Stubs the registry SOURCE (``app.role_registry.load_registry``) so the real
    ``resolve_role`` precedence logic runs against the synthetic registry, and
    also writes the same registry to a temp file + sets
    ``PIPELINE_MODEL_REGISTRY_PATH`` so an implementation that reads the file
    directly observes identical contents. Never touches this machine's real
    model_registry.json.
    """
    for key in [k for k in os.environ if k.startswith("PIPELINE_")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)

    from app import role_registry

    monkeypatch.setattr(
        role_registry, "load_registry", lambda *a, **k: copy.deepcopy(registry)
    )

    registry_file = tmp_path / "synthetic_model_registry.json"
    registry_file.write_text(json.dumps(registry))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(registry_file))


def _expected_provider_model(registry: dict, environ: dict) -> tuple[str, str | None]:
    """What real dispatch resolves for the same synthetic inputs.

    ``resolve_role`` is the canonical resolver; when it raises because no model
    is configured anywhere (the fresh-clone empty state), the provider is still
    well-defined and is read from ``resolve_role_provenance``, which keeps the
    provider and its source label on that error path. The model is then
    unspecified, so callers compare providers only.
    """
    from app import role_registry

    try:
        resolution = role_registry.resolve_role(
            "dispatch", registry=registry, environ=environ
        )
    except role_registry.RoleRegistryError:
        from pipeline.config_provenance import resolve_role_provenance

        provenance = resolve_role_provenance(
            "dispatch", registry=registry, environ=environ
        )
        return provenance["provider"], None
    return resolution.provider, resolution.model


def _announce_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "validating dispatch on" in line]


# --------------------------------------------------------------------------
# 1. POSITIVE - the registry drives the announced backend
# --------------------------------------------------------------------------


def test_registry_pinned_dispatch_drives_the_announced_backend(
    monkeypatch, capsys, tmp_path
):
    """roles.dispatch = ollama/deepseek-v4.1-flash, env unset -> announce ollama.

    This is the measured failure from the story: the smoke used to announce
    claude/sonnet here while real dispatch resolved ollama.
    """
    registry = _registry_with_dispatch(
        "ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud"
    )
    _install(monkeypatch, tmp_path, registry, {})
    mod = _load_script()

    provider, model, _source = mod._announce_dispatch_backend()

    assert provider == "ollama", (
        "with roles.dispatch pinned to ollama and PIPELINE_BACKEND_DISPATCH "
        f"unset, the smoke must announce the registry's provider; got {provider!r}"
    )
    assert model == "deepseek-v4.1-flash:cloud", (
        "the announced model must be the registry's resolved tag; "
        f"got {model!r}"
    )

    captured = capsys.readouterr()
    lines = _announce_lines(captured.out + captured.err)
    assert lines, (
        "the guard must print one prominent announce line naming the resolved "
        f"provider/model; got: {(captured.out + captured.err)!r}"
    )
    line = lines[-1]
    assert "ollama" in line, f"announce line must name ollama; got {line!r}"
    assert "deepseek-v4.1-flash:cloud" in line, (
        f"announce line must name the registry model; got {line!r}"
    )
    assert "claude" not in line, (
        "the announce line must not name claude when the registry pinned "
        f"ollama; got {line!r}"
    )


# --------------------------------------------------------------------------
# 2. PARITY - announced backend == resolve_role("dispatch")
# --------------------------------------------------------------------------


PARITY_CASES = [
    pytest.param(
        _registry_with_dispatch("ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud"),
        {},
        id="registry-pins-ollama-env-unset",
    ),
    pytest.param(
        _registry_with_dispatch("lmstudio", "qwen3-coder", "qwen3-coder@q4"),
        {"PIPELINE_BACKEND_DISPATCH": "ollama"},
        id="registry-pins-lmstudio-env-names-ollama",
    ),
    pytest.param(
        _registry_with_dispatch("claude", "sonnet", "sonnet"),
        {},
        id="registry-pins-claude-env-unset",
    ),
    pytest.param(
        {},
        {"PIPELINE_BACKEND_DISPATCH": "ollama"},
        id="empty-registry-env-names-ollama",
    ),
    pytest.param({}, {}, id="empty-registry-env-unset"),
    pytest.param(
        {},
        {"PIPELINE_BACKEND_DISPATCH": ""},
        id="empty-registry-env-empty",
    ),
    pytest.param(
        _registry_provider_only("ollama"),
        {"PIPELINE_BACKEND_DISPATCH": "lmstudio"},
        id="provider-only-registry-entry-env-names-lmstudio",
    ),
]


@pytest.mark.parametrize("registry,environ", PARITY_CASES)
def test_announced_backend_matches_resolve_role(
    monkeypatch, capsys, tmp_path, registry, environ
):
    """The smoke's resolved (provider, model) must equal resolve_role's."""
    _install(monkeypatch, tmp_path, registry, environ)
    mod = _load_script()

    provider, model, _source = mod._announce_dispatch_backend()
    expected_provider, expected_model = _expected_provider_model(registry, environ)

    assert provider == expected_provider, (
        "the smoke's announced provider must equal "
        "role_registry.resolve_role('dispatch') for the same inputs; "
        f"smoke={provider!r} resolve_role={expected_provider!r} "
        f"(registry={registry!r}, environ={environ!r})"
    )
    if expected_model is not None:
        assert model == expected_model, (
            "the smoke's announced model must equal "
            "role_registry.resolve_role('dispatch') for the same inputs; "
            f"smoke={model!r} resolve_role={expected_model!r}"
        )


# --------------------------------------------------------------------------
# 3. REGISTRY OUTRANKS ENV
# --------------------------------------------------------------------------


def test_registry_outranks_pipeline_backend_dispatch_env(
    monkeypatch, capsys, tmp_path
):
    """registry pins ollama, PIPELINE_BACKEND_DISPATCH names lmstudio -> ollama."""
    registry = _registry_with_dispatch("ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud")
    _install(monkeypatch, tmp_path, registry, {"PIPELINE_BACKEND_DISPATCH": "lmstudio"})
    mod = _load_script()

    provider, model, _source = mod._announce_dispatch_backend()

    assert provider == "ollama", (
        "the registry's roles.dispatch entry outranks "
        "PIPELINE_BACKEND_DISPATCH; got provider "
        f"{provider!r} (env named lmstudio)"
    )
    assert model == "deepseek-v4.1-flash:cloud"
    captured = capsys.readouterr()
    lines = _announce_lines(captured.out + captured.err)
    assert lines and "ollama" in lines[-1], (
        f"announce line must name the registry provider; got {lines!r}"
    )


# --------------------------------------------------------------------------
# 4. BOUNDARY - the empty state
# --------------------------------------------------------------------------


BOUNDARY_CASES = [
    pytest.param(
        {},
        {"PIPELINE_BACKEND_DISPATCH": "ollama"},
        "ollama",
        id="no-registry-entry-env-set-env-wins",
    ),
    pytest.param({}, {}, "claude", id="nothing-set-defaults-to-claude"),
    pytest.param(
        {},
        {"PIPELINE_BACKEND_DISPATCH": ""},
        "claude",
        id="empty-env-value-defaults-to-claude",
    ),
]


@pytest.mark.parametrize("registry,environ,expected_provider", BOUNDARY_CASES)
def test_empty_state_falls_back_to_env_then_claude(
    monkeypatch, capsys, tmp_path, registry, environ, expected_provider
):
    """No roles.dispatch entry: the env var is the fallback, then claude."""
    _install(monkeypatch, tmp_path, registry, environ)
    mod = _load_script()

    provider, _model, _source = mod._announce_dispatch_backend()

    assert provider == expected_provider, (
        "with no roles.dispatch entry the env var is the empty-state fallback "
        f"and claude is the bottom of the chain; got {provider!r}, expected "
        f"{expected_provider!r} (environ={environ!r})"
    )
    captured = capsys.readouterr()
    lines = _announce_lines(captured.out + captured.err)
    assert lines and expected_provider in lines[-1], (
        f"announce line must name {expected_provider!r}; got {lines!r}"
    )


def test_provider_only_registry_entry_outranks_env(monkeypatch, capsys, tmp_path):
    """A provider-only roles.dispatch entry still beats the env var."""
    registry = _registry_provider_only("ollama")
    _install(monkeypatch, tmp_path, registry, {"PIPELINE_BACKEND_DISPATCH": "lmstudio"})
    mod = _load_script()

    provider, _model, _source = mod._announce_dispatch_backend()

    assert provider == "ollama", (
        "a provider-only roles.dispatch entry must still select the registry "
        f"provider over the env var; got {provider!r}"
    )


# --------------------------------------------------------------------------
# source label names the layer that actually won
# --------------------------------------------------------------------------


def test_source_label_names_the_registry_layer(monkeypatch, capsys, tmp_path):
    registry = _registry_with_dispatch("ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud")
    _install(monkeypatch, tmp_path, registry, {})
    mod = _load_script()

    _provider, _model, source = mod._announce_dispatch_backend()

    assert "registry" in source.lower(), (
        "when the registry won, the reported source must name the registry "
        f"layer (not always 'PIPELINE_BACKEND_DISPATCH'); got {source!r}"
    )


def test_source_label_names_the_env_layer(monkeypatch, capsys, tmp_path):
    _install(monkeypatch, tmp_path, {}, {"PIPELINE_BACKEND_DISPATCH": "ollama"})
    mod = _load_script()

    _provider, _model, source = mod._announce_dispatch_backend()

    assert "PIPELINE_BACKEND_DISPATCH" in source or "env" in source.lower(), (
        "when the env var won, the reported source must name the env layer; "
        f"got {source!r}"
    )


def test_source_label_names_the_default_layer(monkeypatch, capsys, tmp_path):
    _install(monkeypatch, tmp_path, {}, {})
    mod = _load_script()

    _provider, _model, source = mod._announce_dispatch_backend()

    assert "default" in source.lower(), (
        "when nothing was configured, the reported source must name the "
        f"default layer; got {source!r}"
    )


# --------------------------------------------------------------------------
# 5. NEGATIVE - exit 2 for empty / unrecognised providers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["bogus", "not-a-provider", "Ollama2"])
def test_unrecognised_provider_still_exits_2_naming_the_value(
    monkeypatch, capsys, value
):
    mod = _load_script()

    with pytest.raises(SystemExit) as excinfo:
        mod._announce_dispatch_backend(value)

    assert excinfo.value.code == 2, (
        f"an unrecognised provider must exit 2; got {excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert value in text, (
        "the exit-2 message must name the offending value so the operator can "
        f"fix it; got {text!r}"
    )


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_or_whitespace_value_still_exits_2(monkeypatch, capsys, value):
    mod = _load_script()

    with pytest.raises(SystemExit) as excinfo:
        mod._announce_dispatch_backend(value)

    assert excinfo.value.code == 2, (
        f"an empty/whitespace-only value must exit 2; got {excinfo.value.code!r}"
    )


def test_unrecognised_env_provider_still_exits_2_naming_the_value(
    monkeypatch, capsys, tmp_path
):
    """PIPELINE_BACKEND_DISPATCH=bogus with no registry entry -> exit 2."""
    _install(monkeypatch, tmp_path, {}, {"PIPELINE_BACKEND_DISPATCH": "bogus"})
    mod = _load_script()

    with pytest.raises(SystemExit) as excinfo:
        mod._announce_dispatch_backend()

    assert excinfo.value.code == 2, (
        "an unrecognised resolved provider must still fail closed with exit 2; "
        f"got {excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "bogus" in text, (
        "the exit-2 message must name the offending value; "
        f"got {text!r}"
    )


# --------------------------------------------------------------------------
# 6. no advisory claim, and the advisory plumbing is gone
# --------------------------------------------------------------------------


def test_output_makes_no_advisory_claim_about_the_registry(
    monkeypatch, capsys, tmp_path
):
    registry = _registry_with_dispatch("ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud")
    _install(monkeypatch, tmp_path, registry, {})
    mod = _load_script()

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    text = (captured.out + captured.err).lower()
    for phrase in (
        "advisory",
        "never real dispatch",
        "only feeds the dashboard",
        "decompose-time sizing",
        "does not affect",
    ):
        assert phrase not in text, (
            f"the guard must not print {phrase!r} any more - the registry IS "
            f"the dispatch source; got: {(captured.out + captured.err)!r}"
        )


def test_advisory_plumbing_is_gone():
    text = _script_text()
    for name in ADVISORY_PLUMBING_NAMES:
        assert name not in text, (
            f"{name!r} exists only to serve the obsolete advisory registry "
            "note and must be removed from scripts/smoke_getting_started.py"
        )


def test_no_false_registry_claims_anywhere_in_the_file():
    text = _script_text().lower()
    for phrase in FALSE_REGISTRY_CLAIMS:
        assert phrase not in text, (
            f"scripts/smoke_getting_started.py still claims {phrase!r}; the "
            "registry is now the source of truth for dispatch, so that claim "
            "is false and must be replaced with the truth"
        )


def test_module_docstring_states_the_registry_is_authoritative():
    doc = _module_docstring()
    lowered = doc.lower()
    assert "registry" in lowered, (
        "the module docstring must state that dispatch resolves through the "
        "registry"
    )
    assert "PIPELINE_BACKEND_" in doc, (
        "the module docstring must name the env var the registry outranks "
        "(PIPELINE_BACKEND_<ROLE>)"
    )
    assert any(word in lowered for word in PRECEDENCE_WORDS), (
        "the module docstring must state the registry outranks "
        f"PIPELINE_BACKEND_<ROLE>; got: {doc!r}"
    )


def test_announce_guard_docstring_states_the_registry_is_authoritative():
    doc = _function_docstring("_announce_dispatch_backend")
    assert doc, "_announce_dispatch_backend must keep a docstring"
    assert "registry" in doc.lower(), (
        "the announce guard's docstring must state that the resolver consults "
        "the registry"
    )
    lowered = doc.lower()
    for phrase in FALSE_REGISTRY_CLAIMS:
        assert phrase not in lowered, (
            f"the announce guard's docstring still claims {phrase!r}; the "
            "registry is now the source of truth for dispatch, so that claim "
            "is false and must be replaced with the truth"
        )


def test_default_dispatch_resolver_still_exists():
    assert "_default_dispatch_resolver" in _script_text(), (
        "_default_dispatch_resolver is the unit that must resolve via "
        "app.role_registry.resolve_role('dispatch'); it must not be deleted"
    )


# --------------------------------------------------------------------------
# preserved behaviour / structure
# --------------------------------------------------------------------------


def test_critical_ordering_rule_preserved():
    doc = _module_docstring()
    assert "CRITICAL ORDERING" in doc, (
        "the module docstring must keep the CRITICAL ORDERING rule"
    )
    assert "PLAN_DIR" in doc and "WORKTREE_ROOT" in doc, (
        "the CRITICAL ORDERING rule must still name PLAN_DIR/WORKTREE_ROOT"
    )
    assert "registry" in doc.lower() and "lazy" in doc.lower(), (
        "the module docstring must still state that the registry is imported "
        "lazily (never at module scope)"
    )


def test_no_pipeline_or_registry_import_at_module_scope():
    text = _script_text()
    tree = _script_tree()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(text, node) or ""
            assert "pipeline" not in segment, (
                "every pipeline.* import must stay lazy, inside a function, "
                f"after the PLAN_DIR/WORKTREE_ROOT env writes; found {segment!r}"
            )
            assert "role_registry" not in segment, (
                "app.role_registry must be imported lazily, inside a function; "
                f"found {segment!r}"
            )


def test_resolver_uses_the_registry_resolution_api():
    """The resolver must go through app.role_registry, not a hand-rolled read."""
    text = _script_text()
    assert any(
        token in text
        for token in ("resolve_role", "resolve_role_provenance", "role_registry")
    ), (
        "the smoke's resolver must resolve dispatch through app.role_registry "
        "(resolve_role / resolve_role_provenance); the hand-rolled, env-only "
        "resolver is exactly the bug this story fixes"
    )


def test_top_level_function_footprint_unchanged():
    tree = _script_tree()
    names = sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert names == [
        "_announce_dispatch_backend",
        "_prepare_scratch_env",
        "main",
        "run_smoke",
    ], (
        "the top-level function footprint must stay exactly "
        f"['_announce_dispatch_backend', '_prepare_scratch_env', 'main', "
        f"'run_smoke']; got {names!r}"
    )


def test_claude_cli_check_does_not_fire_for_a_local_provider(
    monkeypatch, capsys, tmp_path
):
    """A registry-pinned ollama backend must pass --check-preconditions (0)."""
    registry = _registry_with_dispatch("ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud")
    _install(monkeypatch, tmp_path, registry, {})
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mod = _load_script()

    code = mod.main(["--check-preconditions"])

    assert code == 0, (
        "the claude-CLI check must fire ONLY when the resolved provider is "
        f"claude; a registry-pinned ollama backend must pass, got {code!r}"
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "ollama" in text, (
        f"--check-preconditions must name the validated provider; got {text!r}"
    )


def test_claude_cli_check_fires_when_claude_is_resolved(
    monkeypatch, capsys, tmp_path
):
    registry = _registry_with_dispatch("claude", "sonnet", "sonnet")
    _install(monkeypatch, tmp_path, registry, {})
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mod = _load_script()

    code = mod.main(["--check-preconditions"])

    assert code == 1, (
        "when the resolved provider is claude and the claude CLI is missing, "
        f"--check-preconditions must exit 1; got {code!r}"
    )


def test_check_preconditions_flag_preserved():
    assert "--check-preconditions" in _script_text(), (
        "--check-preconditions must be preserved"
    )


def test_pass_line_still_names_provider_and_model():
    text = _script_text()
    assert "PASS: story" in text, "the PASS line must be preserved"
    assert "provider {provider} model {model}" in text, (
        "the PASS line must still name the resolved provider and model"
    )


def test_fail_closed_scratch_guard_preserved():
    text = _script_text()
    assert "SystemExit(5)" in text, (
        "the fail-closed scratch-path guard (exit 5) must be preserved"
    )
    assert "pipeline.paths.PLAN_DIR" in text, (
        "the scratch guard must still re-verify the resolved "
        "pipeline.paths.PLAN_DIR module attribute"
    )


# --------------------------------------------------------------------------
# end-to-end: the exact CLI done-criteria
# --------------------------------------------------------------------------


def _clean_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIPELINE_")}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PIPELINE_SKIP_ENV_FILE"] = "1"
    env.update(extra)
    return env


def test_subprocess_env_opts_out_of_the_developer_env_file():
    env = _clean_env({})
    assert env.get("PIPELINE_SKIP_ENV_FILE") == "1", (
        "the helper must force PIPELINE_SKIP_ENV_FILE=1 so the child never loads "
        "the developer's real .pipeline.env - otherwise a locally configured "
        "registry silently overrides whatever this test intended to isolate"
    )


def _run_cli(extra_env: dict):
    import subprocess

    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check-preconditions"],
        capture_output=True,
        text=True,
        timeout=180,
        env=_clean_env(extra_env),
        check=False,
    )


def test_cli_announces_the_registry_provider(tmp_path):
    """PIPELINE_MODEL_REGISTRY_PATH=<synthetic ollama pin>, env unset -> ollama."""
    registry_file = tmp_path / "synthetic_model_registry.json"
    registry_file.write_text(
        json.dumps(
            _registry_with_dispatch(
                "ollama", "deepseek-v4.1-flash", "deepseek-v4.1-flash:cloud"
            )
        )
    )

    result = _run_cli({"PIPELINE_MODEL_REGISTRY_PATH": str(registry_file)})

    text = result.stdout + result.stderr
    assert result.returncode == 0, (
        "a registry-pinned ollama backend must pass --check-preconditions; "
        f"got exit {result.returncode}: {text!r}"
    )
    assert "ollama" in text, (
        "the CLI must announce the registry's provider (ollama), not claude; "
        f"got: {text!r}"
    )
    assert "validating dispatch on claude" not in text, (
        f"the CLI must not announce claude when the registry pinned ollama: {text!r}"
    )


def test_cli_still_exits_2_for_an_unrecognised_env_provider(tmp_path):
    """PIPELINE_BACKEND_DISPATCH=bogus -> exit 2 (no registry entry)."""
    registry_file = tmp_path / "synthetic_model_registry.json"
    registry_file.write_text(json.dumps({}))

    result = _run_cli(
        {
            "PIPELINE_MODEL_REGISTRY_PATH": str(registry_file),
            "PIPELINE_BACKEND_DISPATCH": "bogus",
        }
    )

    assert result.returncode == 2, (
        "an unrecognised dispatch provider must still fail closed with exit 2; "
        f"got {result.returncode}: {(result.stdout + result.stderr)!r}"
    )
    assert "bogus" in (result.stdout + result.stderr), (
        "the exit-2 message must name the offending value; got "
        f"{(result.stdout + result.stderr)!r}"
    )
