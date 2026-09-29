"""which command builds, tests and lints a repo, by build marker"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.repo_config import (
    RepoConfigError,
    invalid_config_command,
    load_repo_config,
)


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
    repo without a build step must not be blocked by the build gate. The two
    detectors are now symmetric in philosophy: detect_test_command likewise
    refuses to invent a command for a repo with no build system (it returns a
    portable no-op instead), because there is no reasonable universal
    fallback for "build" - and, as the 2026-09-16 npm-ENOENT failure showed,
    none for "test" either."""
    try:
        config = load_repo_config(cwd)
    except RepoConfigError as error:
        return (cwd, invalid_config_command(error))
    if config is not None and "build_cmd" in config:
        return (cwd, list(config["build_cmd"]))

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

    If no recognized build marker exists in cwd or in any immediate
    subdirectory, returns ``(cwd, [sys.executable, "-c", "pass"])`` - a
    portable, shell-free no-op that exits 0. A repo with no build system has
    no test suite to run, so the gate must not block it with a command that
    cannot work: the previous JavaScript-ecosystem fallback exited 254 with
    an ENOENT error on the README-only scratch repos that
    scripts/smoke_getting_started.py creates, failing correctly-completed
    work. This mirrors detect_build_command directly above, which returns
    None for the same reason: there is no reasonable universal fallback for
    a repo that has nothing to test. The no-op stays visible in the record
    (callers persist it into the manifest's last_test_check.cmd), so an
    operator can always see that a no-op ran rather than a real suite.
    """
    try:
        config = load_repo_config(cwd)
    except RepoConfigError as error:
        return (cwd, invalid_config_command(error))
    if config is not None and "test_cmd" in config:
        return (cwd / config.get("test_cwd", "."), list(config["test_cmd"]))

    cmd = _test_command_for(cwd)
    if cmd is not None:
        return cwd, _apply_pytest_collection_overrides(cmd)

    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _test_command_for(child)
        if cmd is not None:
            return child, _apply_pytest_collection_overrides(cmd)

    return cwd, [sys.executable, "-c", "pass"]  # no-op: no build system detected


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
    try:
        config = load_repo_config(cwd)
    except RepoConfigError as error:
        return (cwd, invalid_config_command(error))
    if config is not None and "lint_cmd" in config:
        return (cwd, list(config["lint_cmd"]))

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