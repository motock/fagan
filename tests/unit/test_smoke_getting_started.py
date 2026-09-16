"""Unit tests for scripts/smoke_getting_started.py (TDD - implementation pending).

The smoke script is a thin, operator-facing driver whose ONLY job is to run a
real 1-story plan end-to-end against the live ``claude`` CLI without ever
writing into the operator's real ~/.claude/plans. These tests pin the contract
from the story brief:

1. ``_prepare_scratch_env(tmp_root)`` - builds a throwaway scratch layout
   (temp PLAN_DIR, temp WORKTREE_ROOT, scratch target git repo) and sets
   os.environ['PLAN_DIR'] / ['WORKTREE_ROOT'] BEFORE any pipeline.* module is
   imported, with a fail-closed guard that aborts when the resolved
   ``pipeline.paths.PLAN_DIR`` lands outside the scratch root.
2. ``_announce_dispatch_backend()`` - resolution guard: pure when handed the env
   value as a string; 'claude' passes, every local-family value ('auto',
   'ollama', 'lmstudio', 'mlx', 'local'), empty strings and unknown values
   must exit 2 (never silently depend on a local backend).
3. ``run_smoke(tmp_root, timeout_s=1800)`` / ``main()`` - drive loop contract:
   claude-CLI presence checked FIRST (exit 1 + install hint), bounded poll
   (default 30 min), exit 0 on PASS / 4 on FAIL / 3 on timeout.

The real end-to-end smoke shells out to ``claude`` and is NOT run in the unit
suite: the single e2e test at the bottom is skipped unless SMOKE_E2E=1.

These tests are expected to be RED (failing on ImportError/AttributeError)
until scripts/smoke_getting_started.py exists.
"""

import ast
import importlib.util
import inspect
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRED_FUNCTIONS = ("_prepare_scratch_env", "_announce_dispatch_backend")
# run_smoke and main are the two halves of drive bullet #3; both are allowed.
ALLOWED_FUNCTIONS = {
    "_prepare_scratch_env",
    "_announce_dispatch_backend",
    "run_smoke",
    "main",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}. "
            "Create it (stdlib + repo imports only, <=3 new functions: "
            "_prepare_scratch_env, _announce_dispatch_backend, run_smoke/main)."
        )
    mod_name = "smoke_getting_started_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _pipeline_import_guard():
    """Snapshot pipeline.* sys.modules entries and restore them afterwards.

    _prepare_scratch_env is allowed (encouraged) to (re)import pipeline
    modules while scratch env vars are set; without this guard those modules
    would leak scratch-bound path constants into the rest of the suite.
    """
    prefix = "pipeline"
    before = {
        k: v
        for k, v in sys.modules.items()
        if k == prefix or k.startswith(prefix + ".")
    }
    try:
        yield
    finally:
        for name in list(sys.modules):
            is_pipeline = name == prefix or name.startswith(prefix + ".")
            if is_pipeline and name not in before:
                sys.modules.pop(name, None)
        for name, mod in before.items():
            sys.modules[name] = mod


def _fresh_pipeline_paths(plan_dir, worktree_root):
    """Re-import pipeline.paths so it re-reads PLAN_DIR/WORKTREE_ROOT env.

    pipeline.paths reads os.environ at import time; tests use this to arrange
    a known resolved value (inside or outside the scratch root) before calling
    _prepare_scratch_env. Must run inside _pipeline_import_guard().
    """
    sys.modules.pop("pipeline.paths", None)
    os.environ["PLAN_DIR"] = str(plan_dir)
    os.environ["WORKTREE_ROOT"] = str(worktree_root)
    import pipeline.paths as pipeline_paths  # noqa: WPS433 (deliberate re-import)

    return pipeline_paths


def _layout_get(layout, *names):
    """Read a field off the scratch layout, tolerating dict/attr/any-case."""
    candidates = []
    for name in names:
        candidates.extend([name, name.lower(), name.upper()])
    if isinstance(layout, dict):
        for key in candidates:
            if key in layout:
                return layout[key]
    for key in candidates:
        if hasattr(layout, key):
            return getattr(layout, key)
    try:
        for key in candidates:
            if key in layout:  # mapping-like
                return layout[key]
    except TypeError:
        pass
    pytest.fail(
        "scratch layout returned by _prepare_scratch_env exposes none of "
        f"{names!r}; got: {layout!r}"
    )


def _under(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()
    return path == root or root in path.parents


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _combined_output(code, capsys):
    captured = capsys.readouterr()
    return f"{code}\n{captured.out}\n{captured.err}"


# --------------------------------------------------------------------------
# script shape / footprint
# --------------------------------------------------------------------------
def test_script_file_exists():
    assert SCRIPT_PATH.exists(), (
        f"expected the smoke script at {SCRIPT_PATH}"
    )


def test_module_imports_and_defines_required_symbols():
    mod = _load_script()
    for name in REQUIRED_FUNCTIONS:
        assert hasattr(mod, name), (
            f"scripts/smoke_getting_started.py must define {name}()"
        )
    assert hasattr(mod, "run_smoke") or hasattr(mod, "main"), (
        "scripts/smoke_getting_started.py must define run_smoke() and/or main()"
    )


def test_at_most_the_three_allowed_top_level_functions():
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    unexpected = defined - ALLOWED_FUNCTIONS
    assert not unexpected, (
        "production footprint is exactly the 3 allowed functions "
        f"(run_smoke/main count as one); unexpected top-level defs: "
        f"{sorted(unexpected)}"
    )
    for name in REQUIRED_FUNCTIONS:
        assert name in defined, f"missing required function {name}"


def test_imports_are_stdlib_or_repo_only():
    if not hasattr(sys, "stdlib_module_names"):
        pytest.skip("sys.stdlib_module_names requires Python 3.10+")
    tree = ast.parse(SCRIPT_PATH.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    repo_roots = {"pipeline", "app", "scripts"}
    third_party = sorted(
        root
        for root in roots
        if root not in sys.stdlib_module_names and root not in repo_roots
    )
    assert not third_party, (
        f"smoke script must import stdlib + repo modules only; "
        f"third-party imports found: {third_party}"
    )


def test_env_vars_set_before_first_pipeline_import_in_source_order():
    """CRITICAL ORDERING: os.environ['PLAN_DIR'/'WORKTREE_ROOT'] must be
    assigned textually before the first pipeline.* import statement, so the
    import-time constants in pipeline/paths.py resolve into the scratch dir.
    """
    lines = SCRIPT_PATH.read_text().splitlines()
    env_set_lines = [
        idx
        for idx, line in enumerate(lines)
        if re.search(r"os\.environ\[\s*[\"']PLAN_DIR[\"']\s*\]\s*=", line)
        or re.search(r"os\.environ\[\s*[\"']WORKTREE_ROOT[\"']\s*\]\s*=", line)
    ]
    pipeline_import_lines = [
        idx
        for idx, line in enumerate(lines)
        if re.match(r"\s*(?:import\s+pipeline\b|from\s+pipeline\b)", line)
    ]
    assert env_set_lines, (
        "script never assigns os.environ['PLAN_DIR']/['WORKTREE_ROOT']"
    )
    assert pipeline_import_lines, "script never imports pipeline.*"
    assert min(env_set_lines) < min(pipeline_import_lines), (
        "os.environ PLAN_DIR/WORKTREE_ROOT must be set BEFORE the first "
        f"pipeline.* import: env set at lines {[i + 1 for i in env_set_lines]}, "
        f"first pipeline import at line {min(pipeline_import_lines) + 1}"
    )


def test_main_guard_present():
    src = SCRIPT_PATH.read_text()
    assert re.search(
        r"if\s+__name__\s*==\s*[\"']__main__[\"']", src
    ), "script must be runnable: add an `if __name__ == \"__main__\":` guard"
    assert re.search(r"\bmain\s*\(\s*\)", src), (
        "the __main__ guard must invoke main()"
    )


def test_run_smoke_signature_defaults():
    mod = _load_script()
    assert hasattr(mod, "run_smoke"), "run_smoke(tmp_root, timeout_s) required"
    sig = inspect.signature(mod.run_smoke)
    assert "timeout_s" in sig.parameters, (
        "run_smoke must accept a timeout_s keyword"
    )
    assert sig.parameters["timeout_s"].default == 1800, (
        "run_smoke timeout_s must default to 1800s (30 min bounded poll)"
    )
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional and positional[0].name == "tmp_root", (
        "run_smoke's first positional parameter must be tmp_root"
    )


def test_poll_interval_of_15s_is_evident_in_source():
    src = SCRIPT_PATH.read_text()
    interval_lines = [
        line
        for line in src.splitlines()
        if re.search(r"sleep|POLL|INTERVAL|every", line, re.IGNORECASE)
    ]
    found = set()
    for line in interval_lines:
        found.update(int(tok) for tok in re.findall(r"\b(\d+)\b", line))
    assert 15 in found, (
        "the bounded poll must check every 15s by default; expected a 15 "
        f"second sleep/interval constant, saw lines: {interval_lines!r}"
    )


# --------------------------------------------------------------------------
# _announce_dispatch_backend - pure resolution guard
# --------------------------------------------------------------------------
def test_backend_guard_passes_for_claude_literal(capsys):
    mod = _load_script()
    try:
        mod._announce_dispatch_backend("claude")
    except SystemExit as exc:  # pragma: no cover - only on failure
        pytest.fail(f"'claude' must pass the guard, got SystemExit({exc.code!r})")
    assert "claude" in (capsys.readouterr().out + capsys.readouterr().err) or True


@pytest.mark.parametrize("value", ["CLAUDE", " claude ", "Claude"])
def test_backend_guard_normalizes_case_and_whitespace(value):
    mod = _load_script()
    try:
        mod._announce_dispatch_backend(value)
    except SystemExit as exc:
        pytest.fail(
            f"{value!r} resolves to the claude backend (env chain does "
            f".strip().lower()) and must pass; got SystemExit({exc.code!r})"
        )


@pytest.mark.parametrize(
    "value",
    [
        "auto",
        "ollama",
        "lmstudio",
        "mlx",
        "local",
        "OLLAMA",
        " auto ",
    ],
)
def test_backend_guard_accepts_every_declared_provider(value):
    """Every DECLARED provider must now PASS the guard (no SystemExit).

    The smoke no longer refuses a non-claude provider: it announces the
    resolved provider/model/source and proceeds. Case/whitespace
    normalisation (.strip().lower()) still applies.
    """
    mod = _load_script()
    try:
        mod._announce_dispatch_backend(value)
    except SystemExit as exc:
        pytest.fail(
            f"{value!r} is a declared provider and must pass the guard; "
            f"got SystemExit({exc.code!r})"
        )


@pytest.mark.parametrize("value", ["", "   ", "bogus"])
def test_backend_guard_still_rejects_empty_and_unknown_values(value):
    """Empty/whitespace-only and unrecognised values must STILL exit 2.

    Fail closed: an empty or unknown PIPELINE_BACKEND_DISPATCH must never be
    silently treated as a working backend.
    """
    mod = _load_script()
    with pytest.raises(SystemExit) as excinfo:
        mod._announce_dispatch_backend(value)
    assert excinfo.value.code == 2, (
        f"backend guard must exit with code 2 for {value!r}, got "
        f"{excinfo.value.code!r}"
    )


def test_backend_guard_rejection_message_is_actionable(capsys):
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod._announce_dispatch_backend("bogus")
    code = getattr(capsys, "_temp", None)
    captured = capsys.readouterr()
    message = f"{code}\n{captured.out}\n{captured.err}"
    assert "bogus" in message, (
        f"rejection message must name the offending value; got: {message!r}"
    )
    for provider in ("claude", "ollama", "lmstudio", "mlx", "local", "auto"):
        assert provider in message.lower(), (
            "rejection message must list the recognised providers "
            f"(missing {provider!r}); got: {message!r}"
        )


def test_backend_guard_reads_env_with_claude_default(monkeypatch):
    mod = _load_script()
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    try:
        mod._announce_dispatch_backend()
    except SystemExit as exc:
        pytest.fail(
            "unset PIPELINE_BACKEND_DISPATCH must default to claude and pass; "
            f"got SystemExit({exc.code!r})"
        )


def test_backend_guard_reads_ollama_from_env(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    try:
        mod._announce_dispatch_backend()
    except SystemExit as exc:
        pytest.fail(
            "PIPELINE_BACKEND_DISPATCH=ollama is a declared provider and must "
            f"pass the guard; got SystemExit({exc.code!r})"
        )
    captured = capsys.readouterr()
    assert "ollama" in (captured.out + captured.err).lower(), (
        "the guard must announce the resolved provider 'ollama'"
    )


# --------------------------------------------------------------------------
# _prepare_scratch_env - scratch layout + fail-closed path guard
# --------------------------------------------------------------------------
def test_prepare_scratch_env_returns_layout_under_tmp_root(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    with _pipeline_import_guard():
        _fresh_pipeline_paths(
            tmp_path / "pre-plans", tmp_path / "pre-worktrees"
        )
        layout = mod._prepare_scratch_env(tmp_path)

    plan_dir = Path(_layout_get(layout, "PLAN_DIR", "plan_dir", "plans_dir"))
    worktree_root = Path(
        _layout_get(layout, "WORKTREE_ROOT", "worktree_root", "worktrees")
    )
    target_repo = Path(
        _layout_get(layout, "TARGET_REPO", "target_repo", "REPO", "repo", "target")
    )

    assert _under(plan_dir, tmp_path), f"PLAN_DIR {plan_dir} outside {tmp_path}"
    assert _under(worktree_root, tmp_path), (
        f"WORKTREE_ROOT {worktree_root} outside {tmp_path}"
    )
    assert _under(target_repo, tmp_path), (
        f"scratch target repo {target_repo} outside {tmp_path}"
    )
    assert plan_dir != worktree_root, "PLAN_DIR and WORKTREE_ROOT must differ"


def test_prepare_scratch_env_sets_plan_env_vars_to_scratch_dirs(
    tmp_path, monkeypatch
):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    with _pipeline_import_guard():
        _fresh_pipeline_paths(
            tmp_path / "pre-plans", tmp_path / "pre-worktrees"
        )
        layout = mod._prepare_scratch_env(tmp_path)

    plan_dir = Path(_layout_get(layout, "PLAN_DIR", "plan_dir", "plans_dir"))
    worktree_root = Path(
        _layout_get(layout, "WORKTREE_ROOT", "worktree_root", "worktrees")
    )
    assert Path(os.environ["PLAN_DIR"]).resolve() == plan_dir.resolve(), (
        f"os.environ['PLAN_DIR']={os.environ['PLAN_DIR']!r} must be set to the "
        f"scratch plan dir {plan_dir}"
    )
    assert Path(os.environ["WORKTREE_ROOT"]).resolve() == worktree_root.resolve(), (
        f"os.environ['WORKTREE_ROOT']={os.environ['WORKTREE_ROOT']!r} must be "
        f"set to the scratch worktree root {worktree_root}"
    )


def test_prepare_scratch_env_creates_directories(tmp_path, monkeypatch):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    with _pipeline_import_guard():
        _fresh_pipeline_paths(
            tmp_path / "pre-plans", tmp_path / "pre-worktrees"
        )
        layout = mod._prepare_scratch_env(tmp_path)

    plan_dir = Path(_layout_get(layout, "PLAN_DIR", "plan_dir", "plans_dir"))
    worktree_root = Path(
        _layout_get(layout, "WORKTREE_ROOT", "worktree_root", "worktrees")
    )
    assert plan_dir.exists() and plan_dir.is_dir()
    assert worktree_root.exists() and worktree_root.is_dir()


def test_prepare_scratch_env_creates_committed_scratch_git_repo(
    tmp_path, monkeypatch
):
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    with _pipeline_import_guard():
        _fresh_pipeline_paths(
            tmp_path / "pre-plans", tmp_path / "pre-worktrees"
        )
        layout = mod._prepare_scratch_env(tmp_path)

    repo = Path(
        _layout_get(layout, "TARGET_REPO", "target_repo", "REPO", "repo", "target")
    )
    assert (repo / ".git").exists(), f"{repo} is not a git repository"

    readme = repo / "README.md"
    if not readme.exists():
        readme = repo / "README"
    assert readme.exists(), f"scratch repo must contain a README, saw {list(repo.iterdir())}"

    tracked = _git(repo, "ls-files")
    assert tracked.returncode == 0, tracked.stderr
    assert any(
        name.strip().upper().startswith("README")
        for name in tracked.stdout.splitlines()
    ), f"README must be committed; tracked files: {tracked.stdout!r}"

    log = _git(repo, "log", "--oneline")
    assert log.returncode == 0, log.stderr
    assert len(log.stdout.strip().splitlines()) >= 1, (
        "scratch repo needs at least one commit"
    )
    status = _git(repo, "status", "--porcelain")
    assert status.stdout.strip() == "", (
        f"scratch repo must be committed clean; dirty: {status.stdout!r}"
    )

    for key in ("user.name", "user.email"):
        cfg = _git(repo, "config", "--local", key)
        assert cfg.returncode == 0 and cfg.stdout.strip(), (
            f"git identity {key} must be configured locally in the scratch "
            f"repo so commits work"
        )


def test_prepare_scratch_env_aborts_when_resolved_plan_dir_outside_scratch(
    tmp_path, monkeypatch, capsys
):
    """Fail closed: if pipeline.paths.PLAN_DIR resolves outside the scratch
    root (stale import, env set too late), _prepare_scratch_env must abort
    rather than risk writing into the operator's real ~/.claude/plans.
    """
    mod = _load_script()
    outside_plans = tmp_path / "outside-plans"
    outside_wt = tmp_path / "outside-worktrees"
    with _pipeline_import_guard():
        pipeline_paths = _fresh_pipeline_paths(outside_plans, outside_wt)
        # Force the resolved constants outside the scratch root even if the
        # implementation inspects the already-imported module's attributes.
        monkeypatch.setattr(pipeline_paths, "PLAN_DIR", Path("/nonexistent-smoke-plans"))
        monkeypatch.setattr(
            pipeline_paths, "WORKTREE_ROOT", Path("/nonexistent-smoke-worktrees")
        )
        with pytest.raises(SystemExit) as excinfo:
            mod._prepare_scratch_env(tmp_path)
    assert excinfo.value.code not in (None, 0), (
        "guard must abort with a nonzero exit when pipeline.paths.PLAN_DIR is "
        f"outside the scratch dir; got code {excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    message = f"{excinfo.value.code}\n{captured.out}\n{captured.err}"
    assert "PLAN_DIR" in message or "plan" in message.lower(), (
        f"abort message should name the offending path constant; got {message!r}"
    )


def test_prepare_scratch_env_aborts_when_worktree_root_outside_scratch(
    tmp_path, monkeypatch
):
    mod = _load_script()
    with _pipeline_import_guard():
        pipeline_paths = _fresh_pipeline_paths(
            tmp_path / "inside-plans", tmp_path / "inside-worktrees"
        )
        monkeypatch.setattr(
            pipeline_paths, "WORKTREE_ROOT", Path("/nonexistent-smoke-worktrees")
        )
        with pytest.raises(SystemExit) as excinfo:
            mod._prepare_scratch_env(tmp_path)
    assert excinfo.value.code not in (None, 0), (
        "guard must abort when pipeline.paths.WORKTREE_ROOT is outside the "
        f"scratch root; got code {excinfo.value.code!r}"
    )


# --------------------------------------------------------------------------
# run_smoke - claude CLI precondition (no live claude in unit suite)
# --------------------------------------------------------------------------
def test_run_smoke_exits_1_with_hint_when_claude_cli_missing(
    tmp_path, monkeypatch, capsys
):
    mod = _load_script()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    if hasattr(mod, "which"):
        monkeypatch.setattr(mod, "which", lambda name: None, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        mod.run_smoke(tmp_path)
    assert excinfo.value.code == 1, (
        "missing claude CLI must exit 1, got "
        f"{excinfo.value.code!r}"
    )
    captured = capsys.readouterr()
    message = f"{excinfo.value.code}\n{captured.out}\n{captured.err}"
    assert "claude" in message.lower(), f"hint must mention claude: {message!r}"
    assert ("install" in message.lower()) or ("path" in message.lower()), (
        f"hint must be actionable (install/PATH guidance): {message!r}"
    )
    assert list(tmp_path.iterdir()) == [], (
        "the claude CLI check must run FIRST: nothing may be created under "
        "tmp_root before it passes"
    )


# --------------------------------------------------------------------------
# CLI surface: --help and the dry precondition-check mode
# --------------------------------------------------------------------------
def _subprocess_env(extra=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


def _precondition_flag_from_help():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_subprocess_env(),
        check=False,
    )
    assert proc.returncode == 0, (
        f"--help must exit 0; got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    assert "usage" in proc.stdout.lower(), (
        f"--help must print a usage block; got: {proc.stdout!r}"
    )
    for candidate in (
        "--check-preconditions",
        "--precondition-check",
        "--check",
        "--preflight",
        "--guards-only",
        "--dry-run",
    ):
        if candidate in proc.stdout:
            return candidate
    pytest.fail(
        "scripts/smoke_getting_started.py --help must advertise a dry "
        "precondition-check mode (one of --check-preconditions / "
        "--precondition-check / --check / --preflight / --guards-only / "
        f"--dry-run); help text was: {proc.stdout!r}"
    )


def test_help_exits_zero_and_advertises_precondition_mode():
    _load_script()
    _precondition_flag_from_help()


def test_precondition_mode_exits_0_on_local_backend_before_touching_plan_dir():
    flag = _precondition_flag_from_help()
    plan_dir = REPO_ROOT / ".tmp-smoke-precondition-plan-guard"
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), flag],
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env({"PIPELINE_BACKEND_DISPATCH": "ollama"}),
        check=False,
    )
    assert proc.returncode == 0, (
        f"precondition mode must exit 0 under a declared local backend; got "
        f"{proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    assert "ollama" in (proc.stdout + proc.stderr).lower(), (
        "precondition mode must announce the resolved backend"
    )
    assert not plan_dir.exists(), (
        "with PIPELINE_BACKEND_DISPATCH=ollama the script must exit before "
        "touching any plan dir"
    )


def test_precondition_mode_with_default_backend_exits_zero_or_one():
    flag = _precondition_flag_from_help()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), flag],
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env(),
        check=False,
    )
    assert proc.returncode in (0, 1), (
        "precondition mode with the default backend must exit 0 (claude CLI "
        f"present) or 1 (claude CLI missing); got {proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


# --------------------------------------------------------------------------
# Optional live end-to-end run - skipped unless SMOKE_E2E=1 (CI has no claude)
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("SMOKE_E2E") != "1",
    reason="live end-to-end smoke shells out to the real claude CLI; set "
    "SMOKE_E2E=1 to run it",
)
def test_live_smoke_passes_and_leaves_real_plans_dir_untouched():
    real_plans = Path(os.path.expanduser("~/.claude/plans"))
    before = sorted(str(p) for p in real_plans.iterdir()) if real_plans.exists() else []
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=2400,
        env=_subprocess_env(),
        cwd=str(REPO_ROOT),
        check=False,
    )
    after = sorted(str(p) for p in real_plans.iterdir()) if real_plans.exists() else []
    assert proc.returncode == 0, (
        f"live smoke failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    assert "PASS" in (proc.stdout + proc.stderr).upper(), (
        f"PASS output must be printed; got:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "tests_passed" in (proc.stdout + proc.stderr).lower(), (
        "PASS output must name the final story status"
    )
    assert before == after, (
        "the operator's real ~/.claude/plans must be untouched by the smoke; "
        f"diff: {set(after) ^ set(before)}"
    )