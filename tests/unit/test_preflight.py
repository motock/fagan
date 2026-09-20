"""Tests for pipeline.preflight (TDD: written before the implementation).

preflight is the "can this host run the pipeline?" gate: run_preflight()
returns one dict per check ({"name", "status": "ok"|"warn"|"fail", "message"}),
summarize() renders one human line, and raise_on_failure() turns fail-status
checks into a PreflightError.

Repo rule applied throughout: *test the resolution logic, not today's
configured values*. Every test injects `which` and `registry_loader`, so no
assertion here depends on the live host's installed CLIs or on the current
contents of model_registry.json. Directory checks use tmp_path; the two
chmod-based read-only checks skip gracefully for root and Windows.

Until pipeline/preflight.py exists, this module fails at collection with
ModuleNotFoundError - the expected RED state for this story.
"""

from __future__ import annotations

import ast
import inspect
import io
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

import pipeline.paths
from pipeline import preflight

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPO_ROOT / "pipeline" / "preflight.py"

# A synthetic secret used ONLY to prove preflight messages never leak loader
# error text (the brief's non-leaking assertion). Never a real credential.
_SECRET = "sk-fake-preflight-token-abc123"

_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_WINDOWS = sys.platform.startswith("win")
# chmod-based read-only dirs are meaningless for root (root ignores perms)
# and unsupported on Windows (no POSIX chmod semantics).
_READONLY_UNTESTABLE = _ROOT or _WINDOWS


# --------------------------------------------------------------------------- #
# Stubs: the injected `which` / `registry_loader` doubles used everywhere.
# --------------------------------------------------------------------------- #
def _ok_which(name):
    """`which` stub that reports every CLI as installed.

    Returns sys.executable rather than a fabricated path on the off chance an
    implementation stats the result: sys.executable is guaranteed to exist.
    """
    return sys.executable


def _none_which(name):
    """`which` stub that reports every CLI as absent."""
    return


def _ok_registry():
    """Synthetic registry-loader success (shape mirrors model_registry.json)."""
    return {
        "providers": {"claude": {"models": ["claude-sonnet-4"]}},
        "roles": {"overlord": {"provider": "claude"}},
    }


def _raising_registry():
    raise RuntimeError(f"registry unreadable (PIPELINE_TOKEN={_SECRET})")


def _find(results, *fragments):
    """Return the single check whose name contains any fragment (lowercased)."""
    matches = [
        check
        for check in results
        if any(f in str(check.get("name", "")).lower() for f in fragments)
    ]
    assert matches, (
        f"no check matching {fragments} in names "
        f"{[check.get('name') for check in results]}"
    )
    return matches[0]


def _pin_plan_dir(monkeypatch, target):
    """Point every surface production PLAN_DIR resolution may read at `target`.

    Covers all three blessed resolution strategies so the test is deterministic
    regardless of which one the implementation picked: reading the PLAN_DIR env
    var at call time, reading pipeline.paths.PLAN_DIR at call time (the repo's
    documented patch surface), and a `from pipeline.paths import PLAN_DIR`
    binding captured at preflight import time.
    """
    monkeypatch.setenv("PLAN_DIR", str(target))
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", Path(target))
    if hasattr(preflight, "PLAN_DIR"):
        monkeypatch.setattr(preflight, "PLAN_DIR", Path(target))


# --------------------------------------------------------------------------- #
# Signature / injectability contract.
# --------------------------------------------------------------------------- #
def test_run_preflight_signature_matches_brief():
    sig = inspect.signature(preflight.run_preflight)
    assert list(sig.parameters) == ["plan_dir", "which", "registry_loader"]
    assert sig.parameters["plan_dir"].default is None
    # The default must be the real shutil.which so production resolves live
    # tools while tests can inject a stub.
    assert sig.parameters["which"].default is shutil.which
    assert sig.parameters["registry_loader"].default is None


# --------------------------------------------------------------------------- #
# Happy path: shape of the result list.
# --------------------------------------------------------------------------- #
def test_run_preflight_returns_four_well_formed_checks_all_ok(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    assert isinstance(results, list)
    # One dict per check. Cumulative list: the four briefed checks plus the
    assert isinstance(results, list)
    # One dict per check. Cumulative list: the four briefed checks plus the
    # SCHEDULER_CONFIG startup check (see
    # tests/unit/test_preflight_scheduler_config.py, which locates its entry
    # by name and never pins the total), plus the merge-CI-gate visibility
    # check (CIGATEVISIBLE-1), plus the SCHEDULER_REVISION checkout-revision
    # check (see tests/unit/test_preflight_scheduler_revision.py, which
    # locates its entry by name and never pins the total).
    assert len(results) == 7
    for check in results:
        assert set(check) == {"name", "status", "message"}
        assert check["status"] in {"ok", "warn", "fail"}
        assert isinstance(check["name"], str) and check["name"]
        assert isinstance(check["message"], str) and check["message"]
    assert all(check["status"] == "ok" for check in results)
    names = {check["name"] for check in results}
    assert {"PLAN_DIR", "git"} <= names
    assert any("dispatch" in n.lower() for n in names)
    assert any("registry" in n.lower() for n in names)
    summary = preflight.summarize(results)
    assert "7 ok" in summary
    assert "0 warn" in summary
    assert "0 fail" in summary


# --------------------------------------------------------------------------- #
# Check a: PLAN_DIR.
# --------------------------------------------------------------------------- #
def test_plan_dir_existing_and_writable_is_ok(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    check = _find(results, "plan")
    assert check["status"] == "ok"
    assert isinstance(check["message"], str) and check["message"]


def test_plan_dir_missing_but_creatable_is_warn(tmp_path):
    target = tmp_path / "not_yet_created"
    results = preflight.run_preflight(
        plan_dir=target, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "plan")["status"] == "warn"


@pytest.mark.skipif(
    _READONLY_UNTESTABLE, reason="chmod 0o500 is meaningless for root / on Windows"
)
def test_plan_dir_unwritable_is_fail_with_actionable_message(tmp_path):
    readonly = tmp_path / "readonly_plans"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        results = preflight.run_preflight(
            plan_dir=readonly, which=_ok_which, registry_loader=_ok_registry
        )
    finally:
        readonly.chmod(0o700)
    check = _find(results, "plan")
    assert check["status"] == "fail"
    # "Actionable" minimum: the message must name the directory it could not
    # write to, not just say "unwritable".
    assert str(readonly) in check["message"]


@pytest.mark.skipif(
    _READONLY_UNTESTABLE, reason="chmod 0o500 is meaningless for root / on Windows"
)
def test_plan_dir_uncreatable_when_parent_unwritable_is_fail(tmp_path):
    parent = tmp_path / "readonly_parent"
    parent.mkdir()
    child = parent / "child"
    parent.chmod(0o500)
    try:
        results = preflight.run_preflight(
            plan_dir=child, which=_ok_which, registry_loader=_ok_registry
        )
    finally:
        parent.chmod(0o700)
    assert _find(results, "plan")["status"] == "fail"


def test_plan_dir_pointing_at_a_regular_file_is_fail(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    results = preflight.run_preflight(
        plan_dir=occupied, which=_ok_which, registry_loader=_ok_registry
    )
    check = _find(results, "plan")
    assert check["status"] == "fail"
    assert isinstance(check["message"], str) and check["message"]


def test_plan_dir_resolves_from_PLAN_DIR_env_var(tmp_path, monkeypatch):
    env_dir = tmp_path / "env_plans"
    env_dir.mkdir()  # exists + writable -> the env-resolved dir checks ok
    _pin_plan_dir(monkeypatch, env_dir)
    results = preflight.run_preflight(
        plan_dir=None, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "plan")["status"] == "ok"


@pytest.mark.skipif(_WINDOWS, reason="HOME-driven default resolution is posix-only")
def test_plan_dir_resolves_default_home_claude_plans(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLAN_DIR", raising=False)
    (tmp_path / ".claude").mkdir()  # parent exists + writable
    # Default target ~/.claude/plans does not exist -> warn, deterministically.
    _pin_plan_dir(monkeypatch, tmp_path / ".claude" / "plans")
    results = preflight.run_preflight(
        plan_dir=None, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "plan")["status"] == "warn"


def test_explicit_plan_dir_argument_wins_over_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit_plans"
    explicit.mkdir()  # exists + writable -> ok
    env_dir = tmp_path / "env_plans"  # missing -> would warn if env won
    _pin_plan_dir(monkeypatch, env_dir)
    results = preflight.run_preflight(
        plan_dir=explicit, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "plan")["status"] == "ok"


@pytest.mark.skipif(
    _READONLY_UNTESTABLE, reason="chmod 0o500 is meaningless for root / on Windows"
)
def test_raise_on_failure_names_the_unwritable_plan_dir(tmp_path):
    readonly = tmp_path / "readonly_plans"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        results = preflight.run_preflight(
            plan_dir=readonly, which=_ok_which, registry_loader=_ok_registry
        )
    finally:
        readonly.chmod(0o700)
    check = _find(results, "plan")
    assert check["status"] == "fail"
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.raise_on_failure(results)
    message = str(excinfo.value)
    assert check["name"] in message
    assert str(readonly) in message


# --------------------------------------------------------------------------- #
# Check b: git.
# --------------------------------------------------------------------------- #
def test_git_absent_is_fail_with_install_hint(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_none_which, registry_loader=_ok_registry
    )
    check = _find(results, "git")
    assert check["status"] == "fail"  # required: fail, never warn
    assert "install" in check["message"].lower()


def test_injected_which_is_probed_for_git_and_claude(tmp_path):
    calls = []

    def recording_which(name):
        calls.append(name)
        return sys.executable

    preflight.run_preflight(
        plan_dir=tmp_path, which=recording_which, registry_loader=_ok_registry
    )
    assert "git" in calls
    assert "claude" in calls  # default backend probes the claude CLI


# --------------------------------------------------------------------------- #
# Check c: dispatch backend.
# --------------------------------------------------------------------------- #
def test_claude_backend_absent_is_fail_naming_the_fix(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_none_which, registry_loader=_ok_registry
    )
    check = _find(results, "dispatch", "backend")
    assert check["status"] == "fail"
    lowered = check["message"].lower()
    assert "claude" in lowered
    assert "install" in lowered or "cli" in lowered


@pytest.mark.parametrize("backend", ["ollama", "lmstudio", "mlx", "local", "auto"])
def test_local_family_backend_absent_is_warn_never_fail(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", backend)
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_none_which, registry_loader=_ok_registry
    )
    # Graceful degradation: warn, never fail, never raise.
    assert _find(results, "dispatch", "backend")["status"] == "warn"


def test_ollama_absent_warn_message_names_provider_and_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_none_which, registry_loader=_ok_registry
    )
    check = _find(results, "dispatch", "backend")
    assert check["status"] == "warn"
    lowered = check["message"].lower()
    assert "ollama" in lowered
    assert "install" in lowered or "configur" in lowered


def test_local_backend_with_cli_present_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "dispatch", "backend")["status"] == "ok"


def test_backend_env_value_is_stripped_and_lowercased(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "  CLAUDE  ")
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "dispatch", "backend")["status"] == "ok"

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", " Ollama ")
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_none_which, registry_loader=_ok_registry
    )
    assert _find(results, "dispatch", "backend")["status"] == "warn"


def test_unknown_backend_value_never_crashes_and_never_passes_silently(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "carrier-pigeon")
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    check = _find(results, "dispatch", "backend")
    assert check["status"] in {"warn", "fail"}
    assert "carrier-pigeon" in check["message"]


# --------------------------------------------------------------------------- #
# Check d: model registry.
# --------------------------------------------------------------------------- #
def test_registry_loader_success_is_ok(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    assert _find(results, "registry")["status"] == "ok"


def test_registry_loader_failure_is_fail_names_json_and_never_leaks(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_raising_registry
    )
    check = _find(results, "registry")
    assert check["status"] == "fail"
    # The loader's error *class* must be reported...
    assert "RuntimeError" in check["message"]
    # ...with a hint pointing at the registry file...
    assert "model_registry.json" in check["message"]
    # ...and never the loader error text itself (it carried a fake token).
    assert _SECRET not in check["message"]


def test_preflight_error_for_registry_failure_mentions_json_not_secret(tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_raising_registry
    )
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.raise_on_failure(results)
    message = str(excinfo.value)
    assert "model_registry.json" in message
    assert _SECRET not in message


@pytest.mark.parametrize("bogus", [None, "not-a-dict", 42, ["a", "b"]])
def test_registry_loader_returning_non_dict_never_crashes(bogus, tmp_path):
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=lambda: bogus
    )
    check = _find(results, "registry")
    assert check["status"] in {"ok", "warn", "fail"}
    assert isinstance(check["message"], str) and check["message"]


def test_default_registry_loader_is_wired_and_well_formed(tmp_path):
    # No assertion on today's model_registry.json contents: only that the
    # default wiring produces a legal, well-formed check.
    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=None
    )
    check = _find(results, "registry")
    assert check["status"] in {"ok", "warn", "fail"}
    assert isinstance(check["message"], str) and check["message"]


# --------------------------------------------------------------------------- #
# summarize().
# --------------------------------------------------------------------------- #
def test_summarize_reports_counts_and_warn_detail():
    results = [
        {"name": "PLAN_DIR", "status": "ok", "message": "writable"},
        {"name": "git", "status": "ok", "message": "found"},
        {"name": "dispatch backend", "status": "ok", "message": "claude found"},
        {"name": "ollama", "status": "warn", "message": "ollama absent"},
    ]
    summary = preflight.summarize(results)
    assert isinstance(summary, str)
    assert summary.strip().lower().startswith("preflight")
    assert "3 ok" in summary
    assert "1 warn" in summary
    assert "0 fail" in summary
    assert "ollama" in summary  # the warn detail from the brief's example
    assert "\n" not in summary  # one human line


def test_summarize_empty_results_boundary():
    summary = preflight.summarize([])
    assert "0 ok" in summary
    assert "0 warn" in summary
    assert "0 fail" in summary


def test_summarize_counts_multiple_fails():
    results = [
        {"name": "git", "status": "fail", "message": "absent"},
        {"name": "PLAN_DIR", "status": "fail", "message": "unwritable"},
    ]
    summary = preflight.summarize(results)
    assert "2 fail" in summary
    assert "0 warn" in summary
    assert "0 ok" in summary


def test_summarize_never_contains_an_absolute_home_path(tmp_path):
    home = str(Path.home())
    synthetic = [
        {
            "name": "PLAN_DIR",
            "status": "fail",
            "message": f"unwritable: {Path.home() / 'plans'}",
        },
        {"name": "ollama", "status": "warn", "message": f"ollama absent ({home})"},
        {"name": "git", "status": "ok", "message": home},
    ]
    assert home not in preflight.summarize(synthetic)

    real = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    assert str(Path.home()) not in preflight.summarize(real)


# --------------------------------------------------------------------------- #
# raise_on_failure().
# --------------------------------------------------------------------------- #
def test_raise_on_failure_passes_on_ok_warn_and_empty():
    ok = {"name": "git", "status": "ok", "message": "found"}
    warn = {"name": "dispatch backend", "status": "warn", "message": "ollama absent"}
    assert preflight.raise_on_failure([]) is None
    assert preflight.raise_on_failure([ok]) is None
    assert preflight.raise_on_failure([ok, warn]) is None  # warns never raise


def test_raise_on_failure_lists_every_fail_name_and_message():
    results = [
        {"name": "PLAN_DIR", "status": "fail", "message": "not writable: /x"},
        {"name": "git", "status": "warn", "message": "warn only"},
        {"name": "model registry", "status": "fail", "message": "unreadable"},
        {"name": "dispatch backend", "status": "ok", "message": "fine"},
    ]
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.raise_on_failure(results)
    message = str(excinfo.value)
    assert "PLAN_DIR" in message
    assert "not writable: /x" in message
    assert "model registry" in message
    assert "unreadable" in message


# --------------------------------------------------------------------------- #
# Module hygiene (static scope rules from the brief).
# --------------------------------------------------------------------------- #
def _top_level_import_roots(source):
    """Root modules of module-top imports only (descends into top-level
    If/Try/With wrappers but NOT into function/class bodies)."""
    tree = ast.parse(source)
    roots = set()

    def visit(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.level:
                    roots.add("<relative>")
                elif stmt.module:
                    roots.add(stmt.module.split(".")[0])
            elif isinstance(stmt, (ast.If, ast.Try, ast.TryStar, ast.With)):
                visit(stmt.body)
                visit(stmt.orelse)
                if isinstance(stmt, ast.Try):
                    for handler in stmt.handlers:
                        visit(handler.body)
                    visit(stmt.finalbody)

    visit(tree.body)
    return roots


def test_module_top_level_imports_are_stdlib_only():
    source = PREFLIGHT_PATH.read_text(encoding="utf-8")
    roots = _top_level_import_roots(source)
    allowed = set(sys.stdlib_module_names) | {"pipeline"}
    offenders = sorted(roots - allowed)
    assert not offenders, (
        "pipeline/preflight.py module-top imports must be stdlib-only "
        f"(plus pipeline internals); found: {offenders}"
    )
    assert "app" not in roots, "preflight must never import anything from app/"


def test_module_defines_exactly_the_three_briefed_functions():
    source = PREFLIGHT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    # The three briefed functions must exist; module-level helpers (e.g. a
    # _read_scheduler_fingerprint helper for the SCHEDULER_CONFIG check) are
    # allowed, so this is a superset check, not an exact set.
    assert {"run_preflight", "summarize", "raise_on_failure"} <= set(functions)
    assert "PreflightError" in classes
    assert callable(preflight.run_preflight)
    assert callable(preflight.summarize)
    assert callable(preflight.raise_on_failure)
    assert issubclass(preflight.PreflightError, RuntimeError)
    exported = getattr(preflight, "__all__", None)
    if exported is not None:
        assert {"run_preflight", "summarize", "raise_on_failure"} <= set(exported)


def test_registry_loaded_by_direct_json_parse_with_explanatory_comment():
    source = PREFLIGHT_PATH.read_text(encoding="utf-8")
    # pipeline/role_registry.py does not exist in this repo, so the brief's
    # fallback applies: load and json-parse model_registry.json directly.
    assert re.search(r"\bjson\.(load|loads)\b", source), (
        "pipeline/role_registry.py does not exist, so preflight must json-parse "
        "model_registry.json directly"
    )
    comments = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    assert any("registry" in comment.lower() for comment in comments), (
        "the direct-json-parse decision must be recorded in a comment"
    )


_PROBE = """
import sys
before = set(sys.modules)
import pipeline.preflight
new = set(sys.modules) - before
roots = {m.split(".")[0] for m in new}
bad = sorted(roots - set(sys.stdlib_module_names) - {"pipeline"})
heavy = sorted(roots & {"requests", "yaml", "fastapi", "flask", "httpx",
                        "openai", "anthropic", "numpy", "pandas", "torch"})
app_leak = sorted(m for m in new if m == "app" or m.startswith("app."))
print("BAD:", bad)
print("HEAVY:", heavy)
print("APP:", app_leak)
sys.exit(1 if (bad or heavy or app_leak) else 0)
"""


def test_importing_preflight_pulls_no_heavy_deps_and_nothing_from_app():
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + existing if existing else ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"importing pipeline.preflight pulled forbidden modules or failed:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )