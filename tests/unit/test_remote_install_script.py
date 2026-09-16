"""Tests for ``scripts/remote-install.sh`` and its README documentation.

The script is meant to be piped into ``bash`` (``curl -fsSL <raw-url> | bash``):
it clones this repo into a local directory and runs the existing
``scripts/install.sh`` inside the checkout.

Everything here is exercised through ``subprocess.run`` -- the script is never
imported.  The "remote" repo is a throwaway local git repository created in
``tmp_path`` and referenced through ``FAGAN_REPO_URL``, so these tests never
touch the network.

These tests are RED until ``scripts/remote-install.sh`` exists and the README
gains its ``### One-line install`` subsection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "remote-install.sh"
README = REPO_ROOT / "README.md"

DEFAULT_REPO_URL = "https://github.com/motock/fagan.git"

BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git")

requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def script_path() -> Path:
    """The script under test, failing loudly (not with a traceback) if absent."""
    if not SCRIPT.exists():
        pytest.fail(
            f"{SCRIPT} does not exist yet -- scripts/remote-install.sh is not "
            "implemented (this is the expected RED state before implementation)"
        )
    return SCRIPT


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    assert GIT is not None, "git is required for this helper"
    return subprocess.run(
        [GIT, *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )


def _base_env(tmp_path: Path, **overrides: object) -> dict[str, str]:
    """A clean environment: isolated HOME, no inherited FAGAN_* overrides."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("FAGAN_REPO_URL", None)
    env.pop("FAGAN_INSTALL_DIR", None)
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _run_script(
    script: Path,
    env: dict[str, str],
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Run the script with bash explicitly (so a broken PATH cannot hide bash)."""
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A local git repo standing in for the GitHub remote.

    Its ``scripts/install.sh`` is a trivial stub that drops a marker file next
    to itself, so tests can prove install.sh ran without a real pip install.
    """
    if GIT is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    scripts = repo / "scripts"
    scripts.mkdir()
    install = scripts / "install.sh"
    install.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'touch "$(dirname "$0")/../.install_ran"\n',
        encoding="utf-8",
    )
    install.chmod(0o755)
    (repo / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "initial commit",
        ],
        cwd=repo,
    )
    return repo


# --------------------------------------------------------------------------
# 1. script exists, is executable, has the right shebang
# --------------------------------------------------------------------------


def test_script_exists_and_is_executable(script_path: Path) -> None:
    assert script_path.is_file(), f"{script_path} is not a regular file"
    assert os.access(script_path, os.X_OK), (
        f"{script_path} is not executable -- run `chmod +x scripts/remote-install.sh`"
    )


def test_script_has_env_bash_shebang(script_path: Path) -> None:
    first_line = script_path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", (
        f"unexpected shebang: {first_line!r}"
    )


def test_script_has_no_syntax_errors(script_path: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_script_defaults_match_the_documented_values(script_path: Path) -> None:
    text = script_path.read_text(encoding="utf-8")
    assert DEFAULT_REPO_URL in text, "default FAGAN_REPO_URL is missing"
    assert "$HOME/.fagan" in text, "default FAGAN_INSTALL_DIR ($HOME/.fagan) is missing"
    assert "FAGAN_REPO_URL" in text
    assert "FAGAN_INSTALL_DIR" in text
    assert "git clone" in text
    assert "pull --ff-only" in text


# --------------------------------------------------------------------------
# 2. fresh install (positive)
# --------------------------------------------------------------------------


@requires_git
def test_fresh_install_clones_repo_and_runs_install(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert install_dir.is_dir(), "install directory was not created"
    assert (install_dir / ".git").is_dir(), "install directory is not a git clone"
    assert (install_dir / "scripts" / "install.sh").is_file(), (
        "cloned checkout is missing scripts/install.sh"
    )
    assert (install_dir / ".install_ran").is_file(), (
        "the cloned scripts/install.sh did not run"
    )
    origin = _git(["remote", "get-url", "origin"], cwd=install_dir).stdout.strip()
    assert origin == str(fixture_repo)
    assert "Cloning" in result.stdout
    assert "Running install.sh" in result.stdout


# --------------------------------------------------------------------------
# 3. idempotent re-run (positive)
# --------------------------------------------------------------------------


@requires_git
def test_rerun_updates_instead_of_recloning(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    first = _run_script(script_path, env)
    assert first.returncode == 0, first.stderr

    marker = install_dir / ".install_ran"
    assert marker.is_file()
    marker.unlink()  # prove the second run really re-runs install.sh

    second = _run_script(script_path, env)

    assert second.returncode == 0, f"stderr:\n{second.stderr}\nstdout:\n{second.stdout}"
    assert marker.is_file(), "install.sh did not run again on the second invocation"
    assert "Updating existing install" in second.stdout

    git_dirs = sorted(p for p in install_dir.rglob(".git") if p.is_dir())
    assert git_dirs == [install_dir / ".git"], (
        f"expected exactly one .git directory, found {git_dirs}"
    )
    assert not (install_dir / "fagan").exists(), "re-run nested a clone inside the install dir"
    assert not (install_dir / "fixture-repo").exists(), "re-run nested a clone inside the install dir"

    origin = _git(["remote", "get-url", "origin"], cwd=install_dir).stdout.strip()
    assert origin == str(fixture_repo)


# --------------------------------------------------------------------------
# 4. FAGAN_INSTALL_DIR override
# --------------------------------------------------------------------------


@requires_git
def test_install_dir_env_override_is_respected(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    custom_dir = tmp_path / "custom-location"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=custom_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode == 0, result.stderr
    assert custom_dir.is_dir()
    assert (custom_dir / ".git").is_dir()
    assert (custom_dir / ".install_ran").is_file()
    assert not (tmp_path / "home" / ".fagan").exists(), (
        "script installed to $HOME/.fagan despite FAGAN_INSTALL_DIR override"
    )


# --------------------------------------------------------------------------
# 5. FAGAN_REPO_URL override
# --------------------------------------------------------------------------


@requires_git
def test_repo_url_env_override_is_what_gets_cloned(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)
    assert result.returncode == 0, result.stderr

    origin = _git(["remote", "get-url", "origin"], cwd=install_dir).stdout.strip()
    assert origin == str(fixture_repo), (
        f"cloned origin {origin!r} is not the FAGAN_REPO_URL override"
    )
    assert origin != DEFAULT_REPO_URL
    assert "github.com/motock/fagan" not in origin


# --------------------------------------------------------------------------
# 6. negative: git missing
# --------------------------------------------------------------------------


@requires_git
def test_missing_git_fails_without_creating_install_dir(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    env["PATH"] = str(empty_path)

    result = _run_script(script_path, env)

    assert result.returncode != 0, "script must fail when git is unavailable"
    assert "git not found" in result.stderr, f"stderr was:\n{result.stderr}"
    assert not install_dir.exists(), "install dir was created despite missing git"


# --------------------------------------------------------------------------
# 7. negative: target exists and is not a git repo
# --------------------------------------------------------------------------


@requires_git
def test_existing_non_git_directory_is_refused_and_untouched(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    sentinel = install_dir / "sentinel.txt"
    sentinel.write_text("do not touch\n", encoding="utf-8")

    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    result = _run_script(script_path, env)

    assert result.returncode != 0, "script must refuse a non-git target directory"
    assert "not a git repository" in result.stderr, f"stderr was:\n{result.stderr}"
    assert sentinel.is_file(), "sentinel file was deleted"
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n", (
        "sentinel file was overwritten"
    )
    assert sorted(p.name for p in install_dir.iterdir()) == ["sentinel.txt"], (
        "the pre-existing directory was modified"
    )


# --------------------------------------------------------------------------
# 8. negative: target exists as a git repo with a different origin
# --------------------------------------------------------------------------


@requires_git
def test_existing_repo_with_different_origin_is_refused_and_unchanged(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    other_url = "https://example.com/someone-else/fagan.git"
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _git(["init", "-q"], cwd=install_dir)
    _git(["remote", "add", "origin", other_url], cwd=install_dir)

    config_before = (install_dir / ".git" / "config").read_bytes()
    head_before = (install_dir / ".git" / "HEAD").read_bytes()

    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    result = _run_script(script_path, env)

    assert result.returncode != 0, "script must refuse a repo with a different origin"
    assert "different origin" in result.stderr, f"stderr was:\n{result.stderr}"
    assert other_url in result.stderr, (
        f"error message does not name the mismatched origin:\n{result.stderr}"
    )
    assert (install_dir / ".git" / "config").read_bytes() == config_before, (
        "the existing repo's git config was modified"
    )
    assert (install_dir / ".git" / "HEAD").read_bytes() == head_before, (
        "the existing repo's HEAD was modified"
    )
    assert not (install_dir / "scripts").exists(), "script cloned into the existing repo"


# --------------------------------------------------------------------------
# README documentation (same change, per CLAUDE.md "update docs")
# --------------------------------------------------------------------------


def _readme_lines() -> list[str]:
    assert README.is_file(), f"README.md not found at {README}"
    text = README.read_text(encoding="utf-8")
    assert text.strip(), "README.md is empty"
    return text.splitlines()


def _index_of(lines: list[str], text: str) -> int:
    """Index of the single line whose stripped content equals ``text``."""
    matches = [i for i, line in enumerate(lines) if line.strip() == text]
    assert len(matches) == 1, (
        f"expected exactly one line equal to {text!r}, found {len(matches)}"
    )
    return matches[0]


def test_readme_has_exactly_one_one_line_install_heading() -> None:
    lines = _readme_lines()
    matches = [i for i, line in enumerate(lines) if "One-line install" in line]
    assert len(matches) == 1, (
        f"expected exactly one 'One-line install' line, found {len(matches)}"
    )
    assert lines[matches[0]].strip() == "### One-line install"


def test_readme_one_line_install_sits_after_quickstart_heading() -> None:
    lines = _readme_lines()
    quickstart = [i for i, line in enumerate(lines) if line.strip() == "## Quickstart"]
    assert len(quickstart) == 1, "expected exactly one '## Quickstart' heading"
    qs = quickstart[0]

    subsection = [i for i, line in enumerate(lines) if line.strip() == "### One-line install"]
    assert len(subsection) == 1
    sub = subsection[0]

    assert lines[qs + 1].strip() == "", "expected a blank line after '## Quickstart'"
    assert sub == qs + 2, (
        "'### One-line install' must come immediately after the '## Quickstart' "
        f"heading and its blank line (Quickstart at {qs}, subsection at {sub})"
    )


def test_readme_one_line_install_precedes_existing_quickstart_paragraph() -> None:
    lines = _readme_lines()
    paragraph = "This gets the MCP server registered and a first plan running end-to-end."
    para_idx = _index_of(lines, paragraph)
    sub = _index_of(lines, "### One-line install")
    assert sub < para_idx, "the new subsection must precede the existing paragraph"
    assert lines[para_idx - 1].strip() == "", (
        "expected a blank line before the existing Quickstart paragraph"
    )


def test_readme_one_line_install_documents_the_script_contract() -> None:
    lines = _readme_lines()
    sub = _index_of(lines, "### One-line install")
    paragraph = "This gets the MCP server registered and a first plan running end-to-end."
    para_idx = _index_of(lines, paragraph)
    block = "\n".join(lines[sub:para_idx])

    assert (
        "curl -fsSL https://raw.githubusercontent.com/motock/fagan/master/scripts/remote-install.sh | bash"
        in block
    ), "the one-line curl|bash command is missing"
    assert "FAGAN_INSTALL_DIR" in block, "FAGAN_INSTALL_DIR override is undocumented"
    assert "FAGAN_REPO_URL" in block, "FAGAN_REPO_URL override is undocumented"
    assert "~/.fagan" in block, "the default install location is undocumented"
    assert "git pull --ff-only" in block, "the update-on-re-run behavior is undocumented"
    assert block.count("```bash") == 2, (
        "expected two bash fenced blocks (one-line install + read-it-first)"
    )


def test_readme_manual_clone_steps_are_preserved() -> None:
    text = README.read_text(encoding="utf-8")
    assert "git clone https://github.com/motock/fagan.git" in text, (
        "the existing manual clone step was removed"
    )
    assert "scripts/install.sh" in text


# --------------------------------------------------------------------------
# 9. spec hardening: fail-fast, defaults, update semantics, safety
# --------------------------------------------------------------------------


def test_script_sets_fail_fast_shell_options(script_path: Path) -> None:
    text = script_path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, (
        "the script must start with `set -euo pipefail` so any failure aborts"
    )


def test_script_has_no_destructive_or_interactive_commands(script_path: Path) -> None:
    text = script_path.read_text(encoding="utf-8")
    for forbidden in ("reset --hard", "rm -rf", "push --force", "push -f", "read -p"):
        assert forbidden not in text, (
            f"the installer must never use {forbidden!r} (fail-securely requirement)"
        )


def test_script_has_no_telemetry_or_extra_network_calls(script_path: Path) -> None:
    # Ignore comments: the header legitimately documents the `curl | bash` usage.
    code_lines = [
        line
        for line in script_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    for forbidden in ("curl ", "wget ", "nc ", "telemetry", "analytics"):
        assert forbidden not in code, (
            f"the installer must have no behavior beyond git clone/pull and "
            f"install.sh -- found {forbidden!r}"
        )


@requires_git
def test_default_install_dir_is_home_dot_fagan(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    # FAGAN_INSTALL_DIR deliberately unset: the documented default must apply.
    env = _base_env(tmp_path, FAGAN_REPO_URL=fixture_repo)

    result = _run_script(script_path, env)

    assert result.returncode == 0, result.stderr
    default_dir = tmp_path / "home" / ".fagan"
    assert (default_dir / ".git").is_dir(), "default install dir is not $HOME/.fagan"
    assert (default_dir / ".install_ran").is_file()


@pytest.fixture
def arg_recording_repo(tmp_path: Path) -> Path:
    """A fixture remote whose install.sh records the arguments it was handed."""
    if GIT is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "arg-repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    scripts = repo / "scripts"
    scripts.mkdir()
    install = scripts / "install.sh"
    install.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s" "$*" > "$(dirname "$0")/../.install_args"\n',
        encoding="utf-8",
    )
    install.chmod(0o755)
    (repo / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "initial commit",
        ],
        cwd=repo,
    )
    return repo


@requires_git
def test_install_sh_is_run_with_no_flags(
    script_path: Path, arg_recording_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=arg_recording_repo, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode == 0, result.stderr
    args_file = install_dir / ".install_args"
    assert args_file.is_file(), "the cloned install.sh did not run"
    assert args_file.read_text(encoding="utf-8") == "", (
        "the end-user install path must run install.sh with no flags (never --dev)"
    )


@requires_git
def test_rerun_fast_forwards_new_commits(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    first = _run_script(script_path, env)
    assert first.returncode == 0, first.stderr

    (fixture_repo / "NEW_FILE.txt").write_text("new\n", encoding="utf-8")
    _git(["add", "-A"], cwd=fixture_repo)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "new commit",
        ],
        cwd=fixture_repo,
    )

    second = _run_script(script_path, env)

    assert second.returncode == 0, second.stderr
    assert (install_dir / "NEW_FILE.txt").is_file(), (
        "re-run must `git pull --ff-only` the existing checkout, not skip the update"
    )


@requires_git
def test_non_fast_forward_pull_fails_and_is_not_swallowed(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    first = _run_script(script_path, env)
    assert first.returncode == 0, first.stderr

    # Diverge locally ...
    (install_dir / "README.md").write_text("local divergence\n", encoding="utf-8")
    _git(["add", "-A"], cwd=install_dir)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "local divergence",
        ],
        cwd=install_dir,
    )
    # ... and remotely, on the same file.
    (fixture_repo / "README.md").write_text("remote divergence\n", encoding="utf-8")
    _git(["add", "-A"], cwd=fixture_repo)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "remote divergence",
        ],
        cwd=fixture_repo,
    )

    result = _run_script(script_path, env)

    assert result.returncode != 0, (
        "a non-fast-forward pull must fail the script rather than be swallowed:\n"
        f"{result.stdout}"
    )
    assert result.stderr.strip(), "the failed pull must not be swallowed silently"


@requires_git
def test_failed_clone_leaves_no_partial_install_dir(
    script_path: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    missing_remote = tmp_path / "does-not-exist.git"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=missing_remote, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode != 0, "cloning a nonexistent remote must fail"
    assert result.stderr.strip(), "a failed clone must report on stderr"
    assert not install_dir.exists(), (
        "a failed clone must not leave a half-cloned directory behind that a "
        "re-run would misread as a non-git conflict"
    )


@requires_git
def test_success_summary_names_install_dir_and_readme(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode == 0, result.stderr
    assert str(install_dir) in result.stdout, (
        "the success summary must name the resolved FAGAN_INSTALL_DIR"
    )
    summary = result.stdout.lower()
    assert "readme.md" in summary, (
        "the success summary must point the user at FAGAN_INSTALL_DIR/README.md "
        "for next steps"
    )
    # ... and must NOT invent specific next-step commands (that is the README
    # pointer story's job).
    for invented in ("step 2", "register the mcp server"):
        assert invented not in summary, (
            f"the summary must not invent the specific next step {invented!r}; "
            "point at README.md instead"
        )


@requires_git
def test_runs_non_interactively_when_piped_into_bash(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    # Exactly how `curl -fsSL <url> | bash` feeds the script: bytes on stdin.
    result = subprocess.run(
        [BASH],
        input=script_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=180,
    )

    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert (install_dir / ".git").is_dir()
    assert (install_dir / ".install_ran").is_file()


@pytest.fixture
def repo_without_install_sh(tmp_path: Path) -> Path:
    """A fixture remote that has no scripts/install.sh at all."""
    if GIT is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "no-install-repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    (repo / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(
        [
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test Fixture",
            "commit",
            "-q",
            "-m",
            "initial commit",
        ],
        cwd=repo,
    )
    return repo


@requires_git
def test_repo_without_install_sh_fails_nonzero(
    script_path: Path, repo_without_install_sh: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=repo_without_install_sh, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode != 0, (
        "a checkout without scripts/install.sh must fail, not report success"
    )
    assert result.stderr.strip(), "the missing install.sh must be reported on stderr"


@requires_git
def test_non_git_conflict_exits_one_and_writes_only_to_stderr(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "keepme.txt").write_text("precious\n", encoding="utf-8")
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )

    result = _run_script(script_path, env)

    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert result.stderr.strip(), "the conflict must be reported on stderr"
    assert "error" not in result.stdout.lower(), "errors belong on stderr, not stdout"
    assert (install_dir / "keepme.txt").read_text(encoding="utf-8") == "precious\n"


@requires_git
def test_missing_git_exits_one_with_stderr_only(
    script_path: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    install_dir = tmp_path / "install"
    env = _base_env(
        tmp_path, FAGAN_REPO_URL=fixture_repo, FAGAN_INSTALL_DIR=install_dir
    )
    env["PATH"] = str(empty_path)

    result = _run_script(script_path, env)

    assert result.returncode == 1, f"expected exit 1, got {result.returncode}"
    assert result.stderr.strip(), "missing git must be reported on stderr"
    assert "git" in result.stderr.lower()
    assert "error" not in result.stdout.lower(), "errors belong on stderr, not stdout"
    assert not install_dir.exists(), "must fail before attempting anything else"


def test_shellcheck_is_clean_when_available(script_path: Path) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed in this environment")
    result = subprocess.run(
        [shellcheck, str(script_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
