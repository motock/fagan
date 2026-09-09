"""PP-03: the smoke's backend guard must resolve the EFFECTIVE dispatch backend.

``scripts/smoke_getting_started.py --check-preconditions`` used to read the
``PIPELINE_BACKEND_DISPATCH`` env var directly, so a host whose
``model_registry.json`` routes the ``dispatch`` role at a local provider
(env var unset) got a false green: the guard printed OK and exited 0 while
production's ``app.role_registry.resolve_role("dispatch")`` returned
``("ollama", "glm-5.3-flash:cloud")``.

These tests pin the fixed contract:

- the guard resolves the dispatch role through the same chain production
  uses (``app.role_registry.resolve_role``), not the raw env var;
- exit codes are unchanged: 0 pass, 1 claude CLI missing, 2 the resolved
  backend is local-family/unknown (or cannot be resolved at all);
- the printed message names the resolved provider AND model AND where the
  choice came from (env var vs registry), so the operator can change it;
- ``--check-preconditions`` still creates nothing on disk when the guard
  exits nonzero.

Role resolution is always STUBBED here - no test asserts against the live
registry (the committed model_registry.json is data owned elsewhere).
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REGISTRY_SOURCE = "model_registry.json (roles.dispatch)"
ENV_SOURCE = "env var PIPELINE_BACKEND_DISPATCH"


def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}")
    mod_name = "smoke_getting_started_pp03"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(monkeypatch):
    """The script module with a clean dispatch env (no live registry reads)."""
    mod = _load_script()
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_DEFAULT_MODEL", raising=False)
    return mod


# --------------------------------------------------------------------------
# effective resolution: env unset + registry routes dispatch away from claude
# --------------------------------------------------------------------------
def test_env_unset_registry_routes_dispatch_to_ollama_exits_2(guard, capsys):
    """The headline bug: registry-routed local provider with the env var
    unset must exit 2 (it used to print OK and exit 0)."""
    mod = guard

    def _registry_routes_ollama():
        return ("ollama", "glm-5.3-flash:cloud", REGISTRY_SOURCE)

    with pytest.raises(SystemExit) as excinfo:
        mod._require_claude_backend(resolver=_registry_routes_ollama)
    assert excinfo.value.code == 2, (
        "a registry-routed ollama dispatch backend must exit 2, got "
        f"{excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "ollama" in text, f"message must name the provider; got: {text!r}"
    assert "glm-5.3-flash:cloud" in text, (
        f"message must name the resolved model; got: {text!r}"
    )
    assert "model_registry.json" in text, (
        f"message must say WHERE the choice came from; got: {text!r}"
    )
    assert "claude" in text.lower(), (
        f"message must tell the operator how to fix it; got: {text!r}"
    )


def test_env_unset_nothing_configured_tells_operator_to_choose_provider(
    guard, capsys
):
    """Nothing configured anywhere must NOT silently pass as a claude
    default: the guard's message must tell the operator to choose a
    provider (and how), exiting 2.
    """
    mod = guard

    def _nothing_configured():
        raise mod._DispatchResolutionError(
            "role 'dispatch': no model configured (plan_role_config, "
            "registry, and the caller's fallback are all empty)"
        )

    with pytest.raises(SystemExit) as excinfo:
        mod._require_claude_backend(resolver=_nothing_configured)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    lowered = text.lower()
    assert "choose" in lowered and "provider" in lowered, (
        "message must tell the operator to choose a provider; got: {text!r}"
    )
    assert "PIPELINE_BACKEND_DISPATCH" in text, (
        f"message must name a concrete way to choose it; got: {text!r}"
    )


# --------------------------------------------------------------------------
# unchanged pass/CLI-missing contract
# --------------------------------------------------------------------------
def test_env_claude_cli_present_passes_exit_0(guard, monkeypatch, capsys):
    """env=claude + claude CLI on PATH => precondition mode passes, exit 0."""
    mod = guard
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(
        mod.shutil, "which", lambda name: "/fake/bin/claude" if name == "claude" else None
    )
    assert mod.main(
        ["--check-preconditions"], resolver=lambda: ("claude", "sonnet", ENV_SOURCE)
    ) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "claude" in text
    assert "sonnet" in text, f"OK message must name the model; got: {text!r}"
    assert "PIPELINE_BACKEND_DISPATCH" in text, (
        f"OK message must say WHERE the choice came from; got: {text!r}"
    )


def test_claude_cli_absent_exits_1(guard, monkeypatch, capsys):
    """claude CLI missing => exit 1 (unchanged), even with a claude
    resolution - the backend guard runs first but must not mask exit 1.
    """
    mod = guard
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod.main(
        ["--check-preconditions"], resolver=lambda: ("claude", "sonnet", ENV_SOURCE)
    ) == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "claude" in text.lower(), f"install hint expected; got: {text!r}"


# --------------------------------------------------------------------------
# NEGATIVE: unknown/garbage provider names must be named and rejected
# --------------------------------------------------------------------------
def test_unknown_provider_exits_2_and_names_the_value(guard, capsys):
    mod = guard
    monkey_value = ("bogus-provider", "some-model", ENV_SOURCE)
    with pytest.raises(SystemExit) as excinfo:
        mod._require_claude_backend(resolver=lambda: monkey_value)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "bogus-provider" in text, (
        f"message must name the offending value; got: {text!r}"
    )


def test_local_family_provider_from_registry_exits_2(guard, capsys):
    mod = guard
    with pytest.raises(SystemExit) as excinfo:
        mod._require_claude_backend(
            resolver=lambda: ("lmstudio", "qwen3:8b", REGISTRY_SOURCE)
        )
    assert excinfo.value.code == 2
    text = capsys.readouterr()
    text = text.out + text.err
    assert "lmstudio" in text and "model_registry.json" in text


def test_empty_provider_exits_2(guard):
    """Fail closed: an empty resolved provider is never the claude backend."""
    mod = guard
    with pytest.raises(SystemExit) as excinfo:
        mod._require_claude_backend(resolver=lambda: ("", "", ENV_SOURCE))
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------
# BOUNDARY: --check-preconditions creates nothing on disk on a nonzero guard
# --------------------------------------------------------------------------
def test_check_preconditions_creates_nothing_on_disk(
    guard, monkeypatch, capsys, tmp_path
):
    """When the guard exits nonzero, precondition mode must not create the
    scratch tmp root (nor any plan dirs): the guard runs before any mkdir.
    """
    mod = guard
    sentinel = tmp_path / "scratch-root"
    mkdtemp_calls: list[str] = []

    def _fake_mkdtemp(prefix=None):
        mkdtemp_calls.append(prefix)
        return str(sentinel)

    monkeypatch.setattr(mod.tempfile, "mkdtemp", _fake_mkdtemp)
    monkeypatch.delenv("PLAN_DIR", raising=False)
    monkeypatch.delenv("WORKTREE_ROOT", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        mod.main(
            ["--check-preconditions"],
            resolver=lambda: ("ollama", "glm-5.3-flash:cloud", REGISTRY_SOURCE),
        )
    assert excinfo.value.code == 2
    assert not mkdtemp_calls, (
        "precondition mode must never create a scratch tmp root; "
        f"mkdtemp called with {mkdtemp_calls!r}"
    )
    assert not sentinel.exists(), "the scratch tmp root must not be created"
    assert "PLAN_DIR" not in os.environ and "WORKTREE_ROOT" not in os.environ, (
        "precondition mode must not bind the pipeline env vars"
    )


# --------------------------------------------------------------------------
# import-order contract: app.role_registry is imported lazily, never at
# module scope (pipeline/paths.py reads env at import time - same rule that
# already applies to pipeline.* imports).
# --------------------------------------------------------------------------
def test_app_role_registry_import_stays_lazy():
    tree = ast.parse(SCRIPT_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
            assert not (roots & {"app", "pipeline"}), (
                f"module-scope import of {sorted(roots & {'app', 'pipeline'})} "
                "violates the lazy-import contract (see the module docstring)"
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            assert root not in {"app", "pipeline"}, (
                f"module-scope 'from {node.module} import ...' violates the "
                "lazy-import contract (see the module docstring)"
            )
    # The lazy import site exists inside the guard's resolver.
    source = SCRIPT_PATH.read_text()
    assert "from app.role_registry import" in source, (
        "the guard must resolve the dispatch role through app.role_registry"
    )
    assert "resolve_role(" in source, (
        "the guard must call app.role_registry.resolve_role for the dispatch role"
    )