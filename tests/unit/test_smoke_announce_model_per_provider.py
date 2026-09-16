"""TDD: the smoke's announce line must name a PROVIDER-AWARE model.

Story: ``scripts/smoke_getting_started.py``'s nested
``_default_dispatch_resolver()`` resolves the provider correctly, but its
model half is a single provider-blind line::

    model = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")

``sonnet`` is the CLAUDE chain's default. The guard now ANNOUNCES every
declared provider (claude, ollama, lmstudio, mlx, local, auto), so a local
provider gets announced as e.g. ``ollama/sonnet`` - a confidently false
statement to the operator, who cannot tell whether the smoke exercised their
configured model.

The fix: make the model half provider-aware, mirroring real dispatch.

* claude  -> ``PIPELINE_DEFAULT_MODEL``, else ``sonnet`` (pipeline/config.py's
  ``DEFAULT_MODEL``). This is the CURRENT behavior and must not change.
* every local-family provider (ollama, lmstudio, mlx, local, auto) ->
  ``PIPELINE_LOCAL_MODEL_DEFAULT``, else the local backend's OWN default
  constant (read from its real source of truth, never hardcoded here).

The resolver's contract is otherwise identical: it still returns
``(provider, model, source)``, the model is reported for operator context
ONLY and must NEVER influence the pass/fail decision, and the source string
is unchanged.

These tests are expected to be RED until that change lands (today a local
provider is announced with the claude default ``sonnet``).
"""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The local-family providers: everything that is NOT the claude chain.
LOCAL_FAMILY_PROVIDERS = ("ollama", "lmstudio", "mlx", "local", "auto")

# The script's production footprint is pinned to exactly these top-level defs
# (the resolver is a nested closure, so the fix must stay inside it).
ALLOWED_TOP_LEVEL_FUNCTIONS = {
    "_announce_dispatch_backend",
    "_prepare_scratch_env",
    "run_smoke",
    "main",
}

# A synthetic tag: proves the announced model comes from the env var and not
# from this machine's real configuration.
SYNTHETIC_LOCAL_TAG = "synthetic-local-tag-xyz"
SYNTHETIC_CLAUDE_TAG = "synthetic-claude-tag-abc"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    mod_name = "smoke_getting_started_model_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run_main(mod, argv):
    """Call main(argv), normalising a raised SystemExit into an exit code."""
    try:
        return mod.main(argv)
    except SystemExit as exc:
        return exc.code if exc.code is not None else 0


def _announce_line(text):
    """Return the single 'validating dispatch on ...' announce line."""
    lines = [ln for ln in text.splitlines() if "validating dispatch on" in ln]
    assert len(lines) == 1, (
        "the guard must print exactly ONE prominent announce line naming the "
        f"resolved provider/model/source; got {lines!r}"
    )
    return lines[0]


def _announced_provider_model(line):
    """Parse '<provider>/<model>' out of the announce line."""
    match = re.search(r"validating dispatch on (\S+)/(\S+) \(source:", line)
    assert match is not None, (
        "the announce line must read 'validating dispatch on "
        f"<provider>/<model> (source: ...)'; got {line!r}"
    )
    return match.group(1), match.group(2)


def _local_default_model():
    """The local backend's OWN default model constant (real source of truth).

    Read from the module that defines it (re-exported by app/backend_ollama.py)
    so this test can never drift from the production constant. Never a
    hardcoded tag literal.
    """
    from app.backend_ollama import _LOCAL_DEFAULT_MODEL

    return _LOCAL_DEFAULT_MODEL


def _subprocess_env(extra=None):
    """A clean env: every PIPELINE_* var stripped, PYTHONPATH pointed at repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIPELINE_")}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


def _run_script_subprocess(extra_env):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check-preconditions"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env(extra_env),
        check=False,
    )


# --------------------------------------------------------------------------
# 1. POSITIVE (the bug): a local provider announces ITS configured model
# --------------------------------------------------------------------------
def test_ollama_announces_configured_local_model_not_sonnet(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)
    # The claude chain's default is set to a DIFFERENT sentinel: if the model
    # half is still provider-blind, this is what gets announced.
    monkeypatch.setenv("PIPELINE_DEFAULT_MODEL", SYNTHETIC_CLAUDE_TAG)

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    provider, model = _announced_provider_model(line)

    assert provider == "ollama"
    assert model == SYNTHETIC_LOCAL_TAG, (
        "a local provider must announce PIPELINE_LOCAL_MODEL_DEFAULT, not the "
        f"claude chain's default; got {line!r}"
    )
    assert "sonnet" not in line, (
        f"the announce line must not name the claude default 'sonnet'; got {line!r}"
    )
    assert SYNTHETIC_CLAUDE_TAG not in line, (
        "the announce line must not leak PIPELINE_DEFAULT_MODEL for a local "
        f"provider; got {line!r}"
    )


# --------------------------------------------------------------------------
# 2. Every local-family provider behaves the same
# --------------------------------------------------------------------------
@pytest.mark.parametrize("provider", LOCAL_FAMILY_PROVIDERS)
def test_every_local_family_provider_announces_local_model(
    provider, monkeypatch, capsys
):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", provider)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)
    monkeypatch.setenv("PIPELINE_DEFAULT_MODEL", SYNTHETIC_CLAUDE_TAG)

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    announced_provider, model = _announced_provider_model(line)

    assert announced_provider == provider
    assert model == SYNTHETIC_LOCAL_TAG, (
        f"{provider!r} is a local-family provider and must announce "
        f"PIPELINE_LOCAL_MODEL_DEFAULT; got {line!r}"
    )
    assert "sonnet" not in line, (
        f"{provider!r} must not be announced with the claude default; got {line!r}"
    )


# --------------------------------------------------------------------------
# 3. NEGATIVE CONTROL (must not regress): claude is unchanged
# --------------------------------------------------------------------------
def test_claude_announces_pipeline_default_model(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setenv("PIPELINE_DEFAULT_MODEL", SYNTHETIC_CLAUDE_TAG)
    # A local default must be IGNORED for the claude provider.
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    provider, model = _announced_provider_model(line)

    assert provider == "claude"
    assert model == SYNTHETIC_CLAUDE_TAG, (
        "the claude provider must keep announcing PIPELINE_DEFAULT_MODEL; "
        f"got {line!r}"
    )
    assert SYNTHETIC_LOCAL_TAG not in line, (
        "the claude provider must not pick up PIPELINE_LOCAL_MODEL_DEFAULT; "
        f"got {line!r}"
    )


def test_claude_falls_back_to_sonnet_when_default_model_unset(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.delenv("PIPELINE_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    provider, model = _announced_provider_model(line)

    assert provider == "claude"
    assert model == "sonnet", (
        "with PIPELINE_DEFAULT_MODEL unset the claude provider must fall back "
        f"to 'sonnet' (pipeline/config.py DEFAULT_MODEL); got {line!r}"
    )


# --------------------------------------------------------------------------
# 4. BOUNDARY: local provider with PIPELINE_LOCAL_MODEL_DEFAULT unset
# --------------------------------------------------------------------------
@pytest.mark.parametrize("provider", LOCAL_FAMILY_PROVIDERS)
def test_local_provider_falls_back_to_local_backend_default_constant(
    provider, monkeypatch, capsys
):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", provider)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_DEFAULT_MODEL", raising=False)

    expected = _local_default_model()
    assert expected, "the local backend's default constant must be non-empty"
    assert expected != "sonnet", (
        "precondition: the local backend's default must differ from the claude "
        "fallback, otherwise this boundary test cannot distinguish the two"
    )

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    announced_provider, model = _announced_provider_model(line)

    assert announced_provider == provider
    assert model == expected, (
        f"{provider!r} with PIPELINE_LOCAL_MODEL_DEFAULT unset must announce "
        f"the local backend's own default constant {expected!r}; got {line!r}"
    )
    assert "sonnet" not in line, (
        f"{provider!r} must not fall back to the claude default; got {line!r}"
    )


# --------------------------------------------------------------------------
# 5. BOUNDARY: the model NEVER affects the exit code
# --------------------------------------------------------------------------
def test_nonsense_local_model_still_exits_zero(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "nonsense-model-!!!")
    # No claude CLI: irrelevant for a local provider.
    monkeypatch.setattr(shutil, "which", lambda name: None)

    code = _run_main(mod, ["--check-preconditions"])

    assert code == 0, (
        "the model is operator context, not a gate: a nonsense local model "
        f"must still exit 0 from --check-preconditions; got {code!r}"
    )
    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    _, model = _announced_provider_model(line)
    assert model == "nonsense-model-!!!", (
        "the model is still REPORTED verbatim for operator context; "
        f"got {line!r}"
    )


# --------------------------------------------------------------------------
# CONTRACT: the resolver still returns (provider, model, source)
# --------------------------------------------------------------------------
def test_resolver_returns_provider_model_source_triple(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)

    result = mod._announce_dispatch_backend()

    assert isinstance(result, tuple) and len(result) == 3, (
        f"the guard must return a (provider, model, source) triple; got {result!r}"
    )
    provider, model, source = result
    assert provider == "ollama"
    assert model == SYNTHETIC_LOCAL_TAG
    assert source == "env var PIPELINE_BACKEND_DISPATCH", (
        f"the source string must be unchanged; got {source!r}"
    )


def test_source_string_unchanged_when_dispatch_env_unset(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)

    provider, _model, source = mod._announce_dispatch_backend()

    assert provider == "claude"
    assert "PIPELINE_BACKEND_DISPATCH unset" in source, (
        f"the unset-env source string must be unchanged; got {source!r}"
    )
    assert "claude" in source


# --------------------------------------------------------------------------
# CONTRACT: the model must NOT come from app.role_registry
# --------------------------------------------------------------------------
def test_model_does_not_come_from_role_registry(monkeypatch, capsys):
    """The resolver ignores the registry for provider selection; it must not
    quietly start reading it for the model either."""
    import app.role_registry

    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", SYNTHETIC_LOCAL_TAG)
    monkeypatch.setattr(
        app.role_registry,
        "load_registry",
        lambda: {
            "roles": {
                "dispatch": {
                    "provider": "claude",
                    "model": "registry-sentinel-model",
                }
            }
        },
    )

    mod._announce_dispatch_backend()

    captured = capsys.readouterr()
    line = _announce_line(captured.out + captured.err)
    _, model = _announced_provider_model(line)

    assert model == SYNTHETIC_LOCAL_TAG, (
        "the model must be resolved from the env var, never from "
        f"model_registry.json's roles.dispatch entry; got {line!r}"
    )
    assert "registry-sentinel-model" not in line


# --------------------------------------------------------------------------
# FOOTPRINT: no new top-level defs, no module-scope pipeline/app imports
# --------------------------------------------------------------------------
def test_top_level_functions_are_exactly_the_allowed_four():
    tree = ast.parse(SCRIPT_PATH.read_text())
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert defined == ALLOWED_TOP_LEVEL_FUNCTIONS, (
        "the fix must stay inside the nested resolver: the script's top-level "
        f"defs must be exactly {sorted(ALLOWED_TOP_LEVEL_FUNCTIONS)}; "
        f"got {sorted(defined)}"
    )


def test_no_module_scope_pipeline_or_app_imports():
    """CRITICAL ORDERING: PLAN_DIR/WORKTREE_ROOT env writes must precede the
    first pipeline.* import, so every such import stays lazy."""
    tree = ast.parse(SCRIPT_PATH.read_text())
    roots = set()
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    offenders = sorted(roots & {"pipeline", "app"})
    assert not offenders, (
        "no pipeline.*/app.* import may appear at module scope (see the "
        f"module docstring's CRITICAL ORDERING rule); found {offenders}"
    )


# --------------------------------------------------------------------------
# CLI: the mechanically-checkable done criteria
# --------------------------------------------------------------------------
def test_cli_ollama_names_local_default_and_not_sonnet():
    proc = _run_script_subprocess({"PIPELINE_BACKEND_DISPATCH": "ollama"})
    text = proc.stdout + proc.stderr
    expected = _local_default_model()

    assert proc.returncode == 0, (
        f"a local provider must pass --check-preconditions; got "
        f"{proc.returncode}\n{text}"
    )
    line = _announce_line(text)
    assert expected in line, (
        f"the CLI announce line must name the local default {expected!r}; "
        f"got {line!r}"
    )
    assert "sonnet" not in line, (
        f"the CLI must not announce 'sonnet' for ollama; got {line!r}"
    )


def test_cli_claude_still_names_sonnet():
    proc = _run_script_subprocess({"PIPELINE_BACKEND_DISPATCH": "claude"})
    text = proc.stdout + proc.stderr

    assert proc.returncode in (0, 1), (
        "the claude provider must still resolve (exit 0, or 1 when the claude "
        f"CLI is absent); got {proc.returncode}\n{text}"
    )
    line = _announce_line(text)
    assert "sonnet" in line, (
        f"the claude provider must still announce 'sonnet'; got {line!r}"
    )


def test_cli_bogus_backend_still_exits_2():
    proc = _run_script_subprocess({"PIPELINE_BACKEND_DISPATCH": "bogus"})
    text = proc.stdout + proc.stderr

    assert proc.returncode == 2, (
        f"an unrecognised backend must still exit 2; got {proc.returncode}\n{text}"
    )
    assert "bogus" in text, "the exit-2 message must name the offending value"
