"""Build/test command detection and acceptance scoping.

Pure helpers - no module-level state, no env reads, no I/O beyond reading
build marker files (pom.xml, package.json, pyproject.toml, Cargo.toml, ...)
from a passed-in cwd. Tests exercise them via p.detect_test_command(...),
p._scope_test_cmd_to_acceptance(...), etc., which resolve through the
re-export in pipeline_mcp_server.py.
"""

import ast
import json
import re
import shutil
import subprocess
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _venv_python_for(cwd: Path) -> Path | None:
    """Locate a project venv interpreter for running pytest, or None.

    A git worktree does not contain ``.venv`` (it is gitignored), so a bare
    ``pytest`` run from a worktree resolves to whatever interpreter is on PATH
    — which may be a different Python than the project venv and lack its deps
    (e.g. fastapi). That makes the test gate false-fail on otherwise-green
    work (collection error / import errors), blocking every Python story.

    Resolve the venv via the worktree's git link: ``git rev-parse
    --git-common-dir`` points at the main repo's ``.git``, whose parent holds
    ``.venv``. Also check ``cwd/.venv`` directly for a non-worktree checkout.
    Returns None when no venv is found so the caller falls back to bare
    ``pytest`` (preserving the existing contract for repos without a venv).
    """
    candidates = [cwd / ".venv" / "bin" / "python"]
    try:
        common = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            check=False, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if common:
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = (cwd / common_path).resolve()
            candidates.append(common_path.parent / ".venv" / "bin" / "python")
    except Exception:  # noqa: S110, BLE001 (deliberate fail-open: any git/path failure here just skips this candidate and falls through to the existing-candidates loop, per this function's docstring)
        pass
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _provision_worktree_venv(worktree: Path) -> None:
    """Give a fresh worktree its own fully-provisioned venv, isolated from
    the shared main-repo venv other concurrently-dispatched stories may be
    using.

    Two problems this solves, both observed live 2026-07-24 on
    RUFF-016-ADOPTION: (1) `_venv_python_for` checks a worktree-local
    ``.venv`` BEFORE the shared main-repo one - a story that follows a
    completely reasonable instruction like "install the new version into
    .venv" (with none existing yet, since ``.venv`` is gitignored) creates a
    fresh, minimal venv containing only what it explicitly installed, which
    then silently shadows the fully-configured shared venv for every
    subsequent test run in that worktree - the story's own test gate false-
    fails on missing deps it never touched. (2) absent this, EVERY worktree
    without its own venv resolves to the SAME shared, mutable main-repo
    ``.venv`` - so a story that bumps or installs a dependency (again,
    entirely reasonable) mutates the interpreter every OTHER concurrently-
    dispatched story's test gate also depends on: a silent cross-story
    contamination risk bounded only by MAX_CONCURRENT_AGENTS.

    Best-effort and language-agnostic by the same static-marker convention
    `detect_test_command` uses: only fires for a Python project
    (``pyproject.toml`` or ``setup.py`` present) that declares its own
    dependencies file (``requirements-dev.txt``, falling back to
    ``requirements.txt``). No-ops for any other ecosystem, or a Python
    project with neither file - those are unaffected by this failure mode
    and keep falling back to the shared main-repo venv exactly as before.

    Raises on a real setup failure (network down, no python3, a broken
    requirements file) so the caller's dispatch attempt fails fast and
    retries on the next tick, rather than silently proceeding with a worktree
    that has no working test environment at all.
    """
    if not ((worktree / "pyproject.toml").exists() or (worktree / "setup.py").exists()):
        return
    req = next(
        (name for name in ("requirements-dev.txt", "requirements.txt")
         if (worktree / name).exists()),
        None,
    )
    if req is None:
        return
    subprocess.run(
        ["python3", "-m", "venv", str(worktree / ".venv")],
        cwd=worktree, check=True, capture_output=True, text=True, timeout=120,
    )
    venv_python = worktree / ".venv" / "bin" / "python3"
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "-r", req],
        cwd=worktree, check=True, capture_output=True, text=True, timeout=300,
    )


def _test_command_for(cwd: Path) -> list[str] | None:
    """Return the test command for cwd if a recognized build marker is present."""
    if (cwd / "pom.xml").exists():
        return ["mvn", "test"]
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        return ["./gradlew", "test"]
    if (cwd / "package.json").exists():
        if (cwd / "yarn.lock").exists():
            return ["yarn", "test"]
        return ["npm", "test"]
    if (cwd / "Makefile").exists():
        result = subprocess.run(
            ["grep", "-q", "^test:", "Makefile"], check=False, cwd=cwd, capture_output=True
        )
        if result.returncode == 0:
            return ["make", "test"]
    if (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists():
        venv_python = _venv_python_for(cwd)
        if venv_python is not None:
            return [str(venv_python), "-m", "pytest"]
        return ["pytest"]
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "test"]
    return None


def _build_command_for(cwd: Path) -> list[str] | None:
    """Return the build command for cwd if a recognized build marker
    declares one, or None if this project has no detectable build step (a
    library with no bundling step, a package.json with no "build" script,
    etc). Deliberately conservative/allow-listed - only ecosystems where a
    build step is unambiguous."""
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except ValueError:
            data = {}
        if isinstance(data.get("scripts"), dict) and "build" in data["scripts"]:
            if (cwd / "yarn.lock").exists():
                return ["yarn", "build"]
            return ["npm", "run", "build"]
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "build"]
    return None


def detect_build_command(cwd: Path) -> tuple[Path, list[str]] | None:
    """Detect the build command and directory to run it in, mirroring
    detect_test_command's cwd-then-immediate-subdirectory search. Returns
    None if no recognized build marker declares a build step anywhere - a
    repo without a build step must not be blocked by the build gate (unlike
    detect_test_command, there is no reasonable universal fallback for
    "build")."""
    cmd = _build_command_for(cwd)
    if cmd is not None:
        return cwd, cmd
    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _build_command_for(child)
        if cmd is not None:
            return child, cmd
    return None


def detect_test_command(cwd: Path) -> tuple[Path, list[str]]:
    """Detect the appropriate test command and the directory to run it in.

    Checks cwd first, then falls back to an immediate subdirectory (e.g.
    engine/) for projects where the buildable project does not live at the
    repo root.
    """
    cmd = _test_command_for(cwd)
    if cmd is not None:
        return cwd, _apply_pytest_collection_overrides(cmd)

    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _test_command_for(child)
        if cmd is not None:
            return child, _apply_pytest_collection_overrides(cmd)

    return cwd, ["npm", "test"]  # fallback


def _ruff_config_present(cwd: Path) -> bool:
    """True if an explicit ruff config file/section exists."""
    if (cwd / "ruff.toml").exists() or (cwd / ".ruff.toml").exists():
        return True
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            if "[tool.ruff" in pyproject.read_text():
                return True
        except OSError:
            pass
    return False


def _ruff_declared_as_dependency(cwd: Path) -> bool:
    """True if ruff is named in a requirements file - the signal this repo
    itself actually emits (no ruff.toml/[tool.ruff] anywhere; ruff is pinned
    only in requirements-dev.txt and CI runs a bare ``ruff check .`` with
    ruff's default rules). A config-file-only detection premise misses this
    entirely, which is exactly what happened when detect_lint_command was
    first speced against a false assumption about this repo's own setup."""
    for name in ("requirements-dev.txt", "requirements-test.txt", "requirements.txt"):
        req = cwd / name
        if req.exists():
            try:
                if "ruff" in req.read_text().lower():
                    return True
            except OSError:
                pass
    return False


def _eslint_config_present(cwd: Path) -> bool:
    for name in ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"):
        if (cwd / name).exists():
            return True
    return any(cwd.glob(".eslintrc*"))


def _lint_command_for(cwd: Path) -> list[str] | None:
    """Return the lint command for cwd if a recognized lint signal is
    present AND the tool it names is actually runnable, or None otherwise
    (no signal, or signal present but tool unavailable - fail open in both
    cases, mirroring _test_command_for's allowlist style but broader: the
    opt-in signal is either an explicit config file OR the tool declared as
    an installed dependency, since a real project can lint with a bare
    default ruleset and no config at all)."""
    is_python_project = (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists()
    if is_python_project and (_ruff_config_present(cwd) or _ruff_declared_as_dependency(cwd)):
        venv_python = _venv_python_for(cwd)
        if venv_python is not None:
            ruff_bin = venv_python.parent / "ruff"
            if ruff_bin.exists():
                return [str(ruff_bin), "check", "."]
        which_ruff = shutil.which("ruff")
        if which_ruff:
            return [which_ruff, "check", "."]
        return None  # signal present but no runnable tool - fail open

    if (cwd / "package.json").exists() and _eslint_config_present(cwd):
        return ["npx", "--no-install", "eslint", "."]

    if (cwd / ".golangci.yml").exists() or (cwd / ".golangci.yaml").exists():
        which_golangci = shutil.which("golangci-lint")
        if which_golangci:
            return [which_golangci, "run"]
        return None

    return None


def detect_lint_command(cwd: Path) -> tuple[Path, list[str]] | None:
    """Detect a lint command for cwd, or None if no recognized lint signal
    is present or the detected tool isn't actually available.

    Deliberately cwd-only (no immediate-subdirectory fallback like
    detect_test_command/detect_build_command have) - callers pass the
    worktree root, and lint is a repo-wide concern, not something that
    needs the subproject-discovery heuristic those two use.

    Fail-open throughout, same contract as detect_build_command: a repo
    with no recognized lint signal (or a signal but no runnable tool) must
    not be blocked by a lint gate.
    """
    cmd = _lint_command_for(cwd)
    return (cwd, cmd) if cmd is not None else None


def _apply_pytest_collection_overrides(cmd: list[str]) -> list[str]:
    """When cmd invokes pytest, override an explicit `testpaths` allowlist
    (e.g. pyproject.toml's `[tool.pytest.ini_options] testpaths = [...]`) so
    the gate collects every real test_*.py, while excluding tests/benchmark
    and tests/experiments - the benchmark/experiment harness that was never
    part of the graded CI suite (its own test_harness_*.py files are red by
    design/require fixtures CI doesn't set up, and tests/benchmark/_runs,
    _matrixtest, _realtest, _repro etc. hold per-run experiment artifacts
    pytest can't even import). This mirrors .github/workflows/ci.yml's own
    `--ignore=tests/benchmark --ignore=tests/experiments` exactly.

    A blanket `--ignore=tests` (this function's behavior before this fix)
    excludes the WHOLE tests/ directory - which, after this repo's own
    root-reorg (docs/tests/core source moved under top-level dirs), is where
    every real test file now lives, under tests/unit/. That made this
    override collect zero tests ("no tests ran", pytest exit code 5),
    silently treated as a full-suite failure by every caller (the acceptance
    oracle and the rework "full suite must pass" done-bar in
    scripts/local_agent_oracle.py's _full_suite_result), even when nothing
    was actually broken - discovered live blocking story MODE32-CHAT-RETRY.

    Without this override at all, a pinned testpaths allowlist would also
    silently drop any NEW standalone test_*.py an agent creates - the gate
    would then run only the allowlisted tests, they'd pass, and the story
    would be marked tests_passed even though the agent's own new test file
    is broken or empty (a false positive observed live, story 93fdc371,
    2026-07-20).

    `--override-ini` is a CLI flag pytest applies regardless of which
    directory it's invoked from or what config file is present, unlike a
    sibling pytest.ini/.pytest.ini (which only takes effect for pytest runs
    rooted at that exact directory and would do nothing for, e.g., a nested
    temp worktree with its own pyproject.toml). `--ignore` on a
    non-existent path is a no-op, not an error, so this is safe to apply
    unconditionally even when tests/benchmark or tests/experiments doesn't
    exist.
    """
    if not _is_pytest_cmd(cmd):
        return cmd
    return [
        *cmd, "--override-ini=testpaths=.",
        "--ignore=tests/benchmark", "--ignore=tests/experiments",
    ]


def _acceptance_rel_paths(story: dict[str, Any]) -> list[str]:
    """Return the worktree-root-relative paths of a story's acceptance fixtures."""
    return [entry["path"] for entry in (story.get("acceptance") or [])]


# Integration-wiring signal in agent_instructions: phrases that mean the
# change touches a call site / registration / wiring point, not just a unit.
# Matched case-insensitively. "call site" covers "call-site"/"call site";
# "wire" covers wire/wiring/wired; "register the/it/that" and "registration
# path" cover decorator/registry wiring.
_WIRING_RE = re.compile(
    r"call[ -]?site|wire|registration path|register (?:the|it|that)|"
    r"decorator that registers|at the (?:call ?site|invocation site)",
    re.IGNORECASE,
)

# Evidence that an acceptance fixture grades the *integration* rather than
# just invoking the unit in isolation. If ANY marker appears in the fixture
# source, the isolation-only warning is suppressed (the fixture is doing
# real integration work). Kept broad on purpose: this is a non-blocking
# nudge, so over-suppression (missing a real isolation-only fixture) is
# cheaper than nagging an author whose fixture is fine.
_ACCEPTANCE_INTEGRATION_MARKERS = (
    "monkeypatch", "main(", "subprocess", "capsys", "capfd",
    "assert_called", "mock", "patch(", "@patch", "mocker",
    "read_text", "findall", "re.search", "re.match", "importlib",
    "exec_module", "reload(", "getattr", "setattr", "inspect",
    "traceback", "cli_runner", "test_client", "requests.",
)


def _isolation_only_acceptance_warning(story: dict[str, Any]) -> str | None:
    """Non-blocking heuristic: return a warning message when a story's
    ``agent_instructions`` require integration wiring (e.g. "update the call
    site") but its acceptance fixture appears to test only the unit in
    isolation.

    Why this exists: an acceptance fixture that calls a function directly
    creates a graded path that bypasses the integration wiring. A weak
    executor passes the oracle while skipping the ungraded wiring step, then
    ships dead code — observed live on the harness-targeted-done-nudge story
    (2026-07-28), where the fixture tested ``_no_tool_nudge`` in isolation
    while the brief told the agent to wire it at the call site. The full
    done-bar suite didn't catch it either, because it doesn't grade the call
    site either.

    Returns ``None`` when there is nothing to flag: no wiring signal in the
    instructions, no acceptance fixture, or the fixture carries integration-
    grading evidence (monkeypatch/main()/subprocess/mock/read_text/etc.).
    Purely advisory — never blocks ingest or dispatch.
    """
    instructions = story.get("agent_instructions") or ""
    acceptance = story.get("acceptance") or []
    if not instructions or not acceptance:
        return None
    if not _WIRING_RE.search(instructions):
        return None
    sources = "\n".join(entry.get("source", "") for entry in acceptance)
    if any(marker in sources for marker in _ACCEPTANCE_INTEGRATION_MARKERS):
        return None
    summary = story.get("summary", "?")
    return (
        f"acceptance fixture for {summary!r} looks isolation-only: "
        f"agent_instructions require integration wiring (e.g. a call-site "
        f"change) but the fixture source has no integration-grading evidence "
        f"(no monkeypatch/main()/subprocess/mock/read_text/etc.). A weak "
        f"executor can pass this fixture while skipping the wiring -- add a "
        f"test that exercises the real call path, not just the unit."
    )


# macOS-only tooling an acceptance fixture might shell out to or hardcode a
# path for. Dispatch, the check_story_status done-bar, and the merge-gate
# reverify all run on macOS, but CI runs ubuntu-latest only - so a fixture
# depending on any of these passes every local gate and fails only after the
# PR is open (observed live 2026-07-30 via `plutil`).
_MACOS_ONLY_MARKERS = ("plutil", "sw_vers", "osascript", "/System/Library", "defaults read")


def _platform_locked_fixture_warning(story: dict[str, Any]) -> str | None:
    """Non-blocking heuristic: return a warning message when a story's
    acceptance fixture depends on macOS-only tooling.

    Returns ``None`` when the story has no acceptance fixtures or none of
    them reference a known macOS-only marker. Purely advisory — never blocks
    ingest.
    """
    acceptance = story.get("acceptance") or []
    if not acceptance:
        return None
    sources = "\n".join(entry.get("source", "") for entry in acceptance)
    matched = [marker for marker in _MACOS_ONLY_MARKERS if marker in sources]
    if not matched:
        return None
    summary = story.get("summary", "?")
    return (
        f"acceptance fixture for {summary!r} depends on macOS-only tooling "
        f"({', '.join(matched)}): dispatch grades on macOS but CI runs "
        f"ubuntu-latest, so this fixture cannot pass in CI"
    )


def _is_pytest_cmd(cmd: list[str]) -> bool:
    """True when `cmd` invokes pytest and can accept path arguments for scoping.

    Matches both `["pytest", ...]` and `[python, "-m", "pytest", ...]` forms
    produced by detect_test_command's venv-aware path (pipeline_mcp_server.py
    lines 392-393). Other runners (cargo, npm, mvn, ...) return False.

    Checks the last non-flag token rather than strictly cmd[-1]: detect_test_
    command's own collection-override flags (--override-ini=testpaths=.,
    --ignore=tests/benchmark/_runs, see _apply_pytest_collection_overrides)
    are appended after "pytest", so a strict cmd[-1] check would return False
    on its own output and silently break every downstream caller that scopes
    or extends the command (e.g. _scope_test_cmd_to_acceptance appending
    acceptance paths).
    """
    if not cmd:
        return False
    for token in reversed(cmd):
        if token.startswith("-"):
            continue
        return token == "pytest" or token.endswith("/pytest")
    return False


def _scope_test_cmd_to_acceptance(
    test_cmd: list[str], acceptance_paths: list[str], test_dir: Path
) -> list[str] | None:
    """Return ``test_cmd`` scoped to run ONLY the acceptance fixtures, or
    ``None`` when the runner can't be safely scoped to specific files (caller
    falls back to the full suite — the MBW safety net).

    This closes the FM-A family for non-pytest runners. A story carrying a
    harness-owned ``acceptance`` block must be graded on those oracle files
    alone, not on the implementer's own test file, whose assertions may be
    wrong (observed live: interval_merge_js wrote a correct src/merge.js —
    gt=True — but a buggy merge.test.js; unscoped ``npm test`` ran both and
    rejected correct work). Previously only pytest was scoped (``pytest
    <files>`` accepts path args); cargo/npm fell back to the full suite, so
    every non-pytest benchmark cell was graded on the implementer's own tests.

    Scoping is applied only where it is well-defined and safe; anything we
    can't scope correctly falls back to the full suite (no regression vs. the
    prior behavior for real-project stories using jest/mocha/etc.):

      - pytest: ``[pytest, *paths]`` (path args; unchanged).
      - cargo:  ``cargo test --test <stem>`` per acceptance fixture under
        ``tests/``. cargo names integration tests by file stem
        (``tests/test_acceptance.rs`` -> ``--test test_acceptance``), so this
        runs ONLY the oracle, excluding the implementer's own
        ``tests/test_<name>.rs``. Only applied when every acceptance path is
        a ``tests/*.rs`` integration test.
      - npm/yarn whose package.json ``test`` script IS ``node --test``:
        ``node --test <paths>``. Node's test runner accepts explicit paths.
        Only applied when the script starts with ``node --test`` (jest/mocha
        can't be safely scoped without knowing their filter flags).
    """
    if not test_cmd or not acceptance_paths:
        return None
    if _is_pytest_cmd(test_cmd):
        return [*test_cmd, *acceptance_paths]
    # cargo test --test <stem> ...
    if test_cmd[:2] == ["cargo", "test"]:
        stems: list[str] = []
        for p in acceptance_paths:
            pp = Path(p)
            if pp.suffix == ".rs" and pp.parent.name == "tests":
                stems.append(pp.stem)
            else:
                return None
        args: list[str] = []
        for s in stems:
            args += ["--test", s]
        return ["cargo", "test", *args]
    # npm test / yarn test whose script is `node --test ...`
    if test_cmd[:2] in (["npm", "test"], ["yarn", "test"]):
        pkg = Path(test_dir) / "package.json"
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            test_script = str(scripts.get("test") or "").strip()
        except (OSError, json.JSONDecodeError):
            return None
        if test_script.startswith("node --test"):
            return ["node", "--test", *acceptance_paths]
        return None
    return None


def _added_pytest_test_paths(
    worktree: Path, story_key: str, base_branch: str
) -> list[str]:
    """Return worktree-relative paths of python test files the story's
    branch added or modified under ``tests/``, diffed against base_branch.

    detect_test_command's ``--ignore=tests`` (see
    _apply_pytest_collection_overrides) excludes tests/ from the gated
    pytest run so the benchmark harness's own non-graded fixtures (red by
    design, or needing fixtures CI doesn't set up) don't run in every
    story's gate. But that same exclusion hides a story's own new
    ``tests/test_*.py`` file from the done-bar when the story's deliverable
    lives under tests/ - the model's tests for its own code become
    invisible to both itself and the gate, so a broken implementation can
    still land ``tests_passed`` (Mode 42, REAL-REPO-HARNESS-TASK-AND-DRIVER:
    the model's own test suite for its tests/benchmark/run_real_repo_task.py
    deliverable never ran against the gate, hiding a broken
    run_groundtruth_in_place delegation from the local done-bar).

    Passing these paths explicitly closes the gap without touching the
    exclusion for the rest of tests/: pytest's ``--ignore`` only filters
    paths it discovers on its own during collection, not ones given
    explicitly as positional arguments (verified empirically - ``pytest
    --ignore=tests tests/foo.py`` still collects and runs foo.py).

    Only meant to be called for stories WITHOUT an acceptance block - a
    story with one is intentionally graded on the harness-owned oracle only
    (FM-A, _scope_test_cmd_to_acceptance), and adding the model's own tests
    back in would undermine that.
    """
    branch = f"agent/{story_key.lower()}"
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACM",
             f"{base_branch}...{branch}"],
            check=False, cwd=str(worktree), capture_output=True, text=True,
        )
    except OSError:
        # Worktree path doesn't exist (or `git` isn't runnable) - fail open,
        # same contract as detect_lint_command/detect_build_command.
        return []
    if r.returncode != 0:
        return []
    paths = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if (p.parts and p.parts[0] == "tests" and p.suffix == ".py"
                and (p.name.startswith("test_") or p.name.endswith("_test.py"))):
            paths.append(line)
    return paths


def _run_lint_gate(worktree: Path, test_env: dict) -> dict | None:
    lint = detect_lint_command(worktree)
    if lint is None:
        return None
    lint_dir, cmd = lint
    try:
        result = subprocess.run(
            cmd, check=False, cwd=lint_dir, capture_output=True, text=True, env=test_env
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-2000:],
        "stderr_tail": (result.stderr or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _module_level_function_names(source: str) -> set[str]:
    """Top-level (module-scope) function names defined in `source`. Ignores
    nested defs, closures, and class methods - only a bare module-level
    `def` is a candidate for _find_dead_new_functions, since that's the
    shape of an independently-callable production symbol a call site is
    expected to reference by name."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _last_done_summary(agent_log: Path) -> str:
    """Return the summary text from the LAST "] DONE:" line in agent.log, or
    "" if the agent never reached done. Only the final DONE line reflects
    the current run - a resumed agent appends to the same log across ticks
    (mirrors _last_nonempty_line's resumed-log caution for STEP_CAP_MARKERS).
    local_agent.py's `done` tool prints its summary argument verbatim as
    "[step N] DONE: <summary>"; this is that real signal, not a fictitious
    exit protocol."""
    if not agent_log.exists():
        return ""
    marker = "] DONE:"
    last = ""
    with open(agent_log, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            idx = line.find(marker)
            if idx != -1:
                last = line[idx + len(marker) :].strip()
    return last


# Rebind _run_lint_gate's globals to pipeline.server's namespace so that
# bare-name reads inside the body (e.g. `detect_lint_command`) resolve against
# pipeline.server at call time. This preserves the original behavior where the
# function lived in pipeline.server and saw monkeypatched module globals
# (LOAD_GLOBAL does not consult a module-level __getattr__, so a plain
# re-export would not).
from . import server as _server

_run_lint_gate = types.FunctionType(
    _run_lint_gate.__code__,
    _server.__dict__,
    _run_lint_gate.__name__,
    _run_lint_gate.__defaults__,
    _run_lint_gate.__closure__,
)


__all__ = [
    "_acceptance_rel_paths",
    "_added_pytest_test_paths",
    "_build_command_for",
    "_is_pytest_cmd",
    "_last_done_summary",
    "_module_level_function_names",
    "_provision_worktree_venv",
    "_run_lint_gate",
    "_scope_test_cmd_to_acceptance",
    "_test_command_for",
    "_venv_python_for",
    "detect_build_command",
    "detect_lint_command",
    "detect_test_command",
]