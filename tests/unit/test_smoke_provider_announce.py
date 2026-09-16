"""TDD: the smoke's dispatch guard must ANNOUNCE and PROCEED, not refuse.

Story: ``scripts/smoke_getting_started.py::_announce_dispatch_backend`` is the
last provider lock-in in the user-facing path. A dispatch backend that
resolves to any DECLARED provider (claude, ollama, lmstudio, mlx, local,
auto) must now PASS the guard after printing ONE prominent line naming the
resolved provider, the resolved model and the source of the choice (reusing
the ``(provider, model, source)`` triple the resolver already returns).

Exit 2 is RESERVED for a genuinely unusable value: an empty/whitespace-only
string or an unrecognised provider name - a real configuration error that
must still fail closed, naming the offending value and listing the recognised
providers.

The claude-CLI-on-PATH check (exit 1) must now apply ONLY when the resolved
provider is claude: a local-backend operator does not need the claude CLI.

These tests are expected to be RED until that change lands (today the guard
raises ``SystemExit(2)`` for every non-claude provider).
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

DECLARED_PROVIDERS = ("claude", "ollama", "lmstudio", "mlx", "local", "auto")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    mod_name = "smoke_getting_started_under_test"
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


def _line_naming(text, *tokens):
    """Return the first line containing every token, else None."""
    for line in text.splitlines():
        if all(token in line for token in tokens):
            return line
    return None


def _subprocess_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIPELINE_")}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


def test_subprocess_env_opts_out_of_the_developer_env_file():
    env = _subprocess_env()
    assert env.get("PIPELINE_SKIP_ENV_FILE") == "1", (
        "the helper must force PIPELINE_SKIP_ENV_FILE=1 so the child never loads "
        "the developer's real .pipeline.env - otherwise a locally configured "
        "registry silently overrides whatever this test intended to isolate"
    )


def _exit_codes_block(doc):
    match = re.search(r"Exit codes:(.*?)(?:\n\n|\nUsage:)", doc, re.DOTALL)
    assert match, "module docstring must keep an 'Exit codes:' table"
    return match.group(1)


def _exit_line(block, code):
    match = re.search(rf"^\s*{code}\b.*$", block, re.MULTILINE)
    assert match, f"exit-code table must still document exit {code}"
    return match.group(0)


# --------------------------------------------------------------------------
# POSITIVE: a declared provider is announced, then the guard proceeds
# --------------------------------------------------------------------------
def test_guard_announces_and_proceeds_for_ollama(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    monkeypatch.setenv("PIPELINE_DEFAULT_MODEL", "glm-5.3-flash:cloud")

    mod._announce_dispatch_backend()  # must NOT raise

    captured = capsys.readouterr()
    text = captured.out + captured.err
    line = _line_naming(text, "ollama", "glm-5.3-flash:cloud", "PIPELINE_BACKEND_DISPATCH")
    assert line is not None, (
        "the guard must print ONE prominent line naming the resolved provider "
        "('ollama'), the resolved model ('glm-5.3-flash:cloud') and the source "
        f"of the choice; got stdout={captured.out!r} stderr={captured.err!r}"
    )


def test_guard_announces_the_resolver_triple(capsys):
    """The announce reuses the (provider, model, source) triple verbatim."""
    mod = _load_script()
    mod._announce_dispatch_backend(
        resolver=lambda: ("lmstudio", "qwen3-coder", "custom resolver")
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert _line_naming(text, "lmstudio", "qwen3-coder", "custom resolver"), (
        "the announce must name the provider, model and source the resolver "
        f"returned; got stdout={captured.out!r} stderr={captured.err!r}"
    )


@pytest.mark.parametrize("provider", DECLARED_PROVIDERS)
def test_guard_accepts_every_declared_provider(provider, monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", provider)
    try:
        mod._announce_dispatch_backend()
    except SystemExit as exc:
        pytest.fail(
            f"{provider!r} is a DECLARED provider and must pass the guard; "
            f"got SystemExit({exc.code!r})"
        )
    captured = capsys.readouterr()
    assert provider in (captured.out + captured.err).lower(), (
        f"the announce must name the resolved provider {provider!r}"
    )


@pytest.mark.parametrize("value,provider", [("OLLAMA", "ollama"), (" auto ", "auto")])
def test_guard_normalizes_case_and_whitespace_for_declared_providers(
    value, provider, monkeypatch, capsys
):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", value)
    try:
        mod._announce_dispatch_backend()
    except SystemExit as exc:
        pytest.fail(
            f"{value!r} normalises to {provider!r} (.strip().lower()) and must "
            f"pass; got SystemExit({exc.code!r})"
        )
    captured = capsys.readouterr()
    assert provider in (captured.out + captured.err).lower(), (
        f"the announce must name the normalised provider {provider!r}"
    )


def test_guard_defaults_to_claude_when_env_unset(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.setenv("PIPELINE_DEFAULT_MODEL", "sonnet")
    mod._announce_dispatch_backend()  # must NOT raise
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert _line_naming(text, "claude", "sonnet", "PIPELINE_BACKEND_DISPATCH"), (
        "an unset PIPELINE_BACKEND_DISPATCH resolves to claude and must be "
        f"announced with its source; got stdout={captured.out!r} "
        f"stderr={captured.err!r}"
    )


# --------------------------------------------------------------------------
# NEGATIVE: empty / unknown values still fail closed with exit 2
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "   ", "bogus"])
def test_guard_still_rejects_empty_and_unknown_values(value, capsys):
    """Fail closed: an empty or unknown value is a real config error.

    It must never be silently treated as a working backend.
    """
    mod = _load_script()
    with pytest.raises(SystemExit) as excinfo:
        mod._announce_dispatch_backend(value)
    assert excinfo.value.code == 2, (
        f"the guard must exit 2 for {value!r}, got {excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    message = captured.out + captured.err
    if value.strip():
        assert value in message, (
            f"the exit-2 message must name the offending value {value!r}; "
            f"got: {message!r}"
        )
    else:
        assert ("empty" in message.lower()) or (repr(value) in message), (
            "the exit-2 message must name the offending (empty/whitespace) "
            f"value; got: {message!r}"
        )
    for provider in DECLARED_PROVIDERS:
        assert provider in message.lower(), (
            f"the exit-2 message must list the recognised providers "
            f"(missing {provider!r}); got: {message!r}"
        )


# --------------------------------------------------------------------------
# BOUNDARY: the claude-CLI check fires ONLY for the claude provider
# --------------------------------------------------------------------------
def test_claude_cli_check_fires_only_for_claude_provider(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no claude CLI
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")

    code = _run_main(mod, ["--check-preconditions"])

    assert code == 0, (
        "with a local provider resolved and the claude CLI absent, the "
        f"precondition check must exit 0, not {code!r} (exit 1 is reserved "
        "for the claude provider)"
    )
    captured = capsys.readouterr()
    assert "ollama" in (captured.out + captured.err).lower(), (
        "the precondition check must announce the resolved provider"
    )


def test_claude_cli_check_still_fires_for_claude_provider(monkeypatch):
    mod = _load_script()
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no claude CLI
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")

    code = _run_main(mod, ["--check-preconditions"])

    assert code == 1, (
        "with the claude provider resolved and the claude CLI absent, the "
        f"precondition check must still exit 1; got {code!r}"
    )


def test_cli_precondition_mode_exits_2_for_unknown_backend():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check-preconditions"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env({"PIPELINE_BACKEND_DISPATCH": "bogus"}),
        check=False,
    )
    assert proc.returncode == 2, (
        f"an unrecognised backend must exit 2; got {proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "bogus" in (proc.stdout + proc.stderr), (
        "the exit-2 message must name the offending value"
    )


# --------------------------------------------------------------------------
# DOCSTRING: the exit-code table and the honest caveat
# --------------------------------------------------------------------------
def test_module_docstring_exit_code_table_matches_new_contract():
    doc = _load_script().__doc__ or ""
    block = _exit_codes_block(doc)

    assert "local-family" not in block.lower(), (
        "the exit-2 row must no longer claim a local-family backend is "
        f"refused; got: {block!r}"
    )
    assert re.search(r"empty", block, re.IGNORECASE), (
        f"the exit-2 row must name the empty-string case; got: {block!r}"
    )
    assert re.search(r"unknown", block, re.IGNORECASE), (
        f"the exit-2 row must name the unknown-provider case; got: {block!r}"
    )

    line1 = _exit_line(block, "1")
    assert "claude" in line1.lower(), (
        f"the exit-1 row must still describe the claude CLI check; got: {line1!r}"
    )
    assert re.search(r"only|provider", line1, re.IGNORECASE), (
        "the exit-1 row must scope the claude-CLI check to the claude "
        f"provider; got: {line1!r}"
    )


def test_module_docstring_states_configured_model_caveat():
    doc = _load_script().__doc__ or ""
    low = doc.lower()

    assert re.search(r"^\s*4\b", doc, re.MULTILINE), "exit 4 must stay documented"
    assert "model" in low, "the caveat must talk about the configured model"
    assert "configur" in low, (
        "the caveat must say PASS depends on the CONFIGURED model"
    )
    assert re.search(r"could not|cannot|unable|failed to|not able", low), (
        "the caveat must say the configured model could not complete the story"
    )
    assert re.search(r"(not|rather than|isn't|is not)[^.\n]{0,60}pipeline", low), (
        "the caveat must say an exit 4 is NOT the pipeline being broken"
    )
    assert re.search(r"pin|lock|surpris|expect", low), (
        "the caveat must name the cost of not pinning a provider (and that "
        "operators must not be surprised by it)"
    )


def test_pass_line_names_provider_and_model_in_source():
    """The final PASS line must be self-describing (provider + model)."""
    source = SCRIPT_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and node.args
            and isinstance(node.args[0], ast.JoinedStr)
        ):
            continue
        literal = "".join(
            value.value
            for value in node.args[0].values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
        if not literal.startswith("PASS:"):
            continue
        segment = (ast.get_source_segment(source, node) or "").lower()
        assert ("provider" in segment) or ("backend" in segment), (
            "the PASS line must name the validated provider; "
            f"got: {segment!r}"
        )
        assert "model" in segment, (
            "the PASS line must name the validated model; "
            f"got: {segment!r}"
        )
        return
    pytest.fail("run_smoke must print a PASS line starting with 'PASS:'")
