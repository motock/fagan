"""Build/test command detection and acceptance scoping.

Pure helpers - no module-level state, no env reads, no I/O beyond reading
build marker files (pom.xml, package.json, pyproject.toml, Cargo.toml, ...)
from a passed-in cwd. Tests exercise them via p.detect_test_command(...),
p._scope_test_cmd_to_acceptance(...), etc., which resolve through the
re-export in pipeline_mcp_server.py.
"""

import json
import subprocess
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
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if common:
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = (cwd / common_path).resolve()
            candidates.append(common_path.parent / ".venv" / "bin" / "python")
    except Exception:
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
            ["grep", "-q", "^test:", "Makefile"], cwd=cwd, capture_output=True
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
        return cwd, cmd

    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _test_command_for(child)
        if cmd is not None:
            return child, cmd

    return cwd, ["npm", "test"]  # fallback


def _acceptance_rel_paths(story: dict[str, Any]) -> list[str]:
    """Return the worktree-root-relative paths of a story's acceptance fixtures."""
    return [entry["path"] for entry in (story.get("acceptance") or [])]


def _is_pytest_cmd(cmd: list[str]) -> bool:
    """True when `cmd` invokes pytest and can accept path arguments for scoping.

    Matches both `["pytest", ...]` and `[python, "-m", "pytest", ...]` forms
    produced by detect_test_command's venv-aware path (pipeline_mcp_server.py
    lines 392-393). Other runners (cargo, npm, mvn, ...) return False.
    """
    if not cmd:
        return False
    last = cmd[-1]
    return last == "pytest" or last.endswith("/pytest")


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
        except Exception:
            return None
        if test_script.startswith("node --test"):
            return ["node", "--test", *acceptance_paths]
        return None
    return None


__all__ = [
    "_venv_python_for",
    "_test_command_for",
    "_build_command_for",
    "detect_build_command",
    "detect_test_command",
    "_acceptance_rel_paths",
    "_is_pytest_cmd",
    "_scope_test_cmd_to_acceptance",
]