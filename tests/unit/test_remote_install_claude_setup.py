"""``scripts/remote-install.sh`` finishes the Claude Code setup, not just the clone.

The one-line installer used to stop after ``install.sh`` and point at the README,
leaving three manual steps: register the MCP server, copy the persona subagents,
copy the overlord policy. It now does all three itself, and never overwrites
anything the user already has: an existing persona or policy file is kept, and an
already-registered ``pipeline`` MCP server is left alone.

Everything runs through ``subprocess.run`` against a throwaway local "remote" repo,
with a stub ``claude`` on ``PATH`` that records its arguments -- the real Claude
Code CLI is never invoked and the network is never touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "remote-install.sh"
README = REPO_ROOT / "README.md"
DEMO = REPO_ROOT / "docs" / "DEMO.md"

BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

STUB_CLAUDE = """#!/usr/bin/env bash
echo "$*" >> "$CLAUDE_STUB_LOG"
if [ "$1 $2" = "mcp get" ]; then exit "${CLAUDE_GET_EXIT:-1}"; fi
if [ "$1 $2" = "mcp add" ]; then exit "${CLAUDE_ADD_EXIT:-0}"; fi
exit 0
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_remote(tmp_path: Path, with_claude_files: bool = True) -> Path:
    """A local repo standing in for GitHub; its install.sh creates the venv python."""
    repo = tmp_path / "remote"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'root="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'mkdir -p "$root/.venv/bin"\n'
        "printf '#!/bin/sh\\n' > \"$root/.venv/bin/python3\"\n"
        'chmod +x "$root/.venv/bin/python3"\n',
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    if with_claude_files:
        (repo / "agents").mkdir()
        (repo / "agents" / "code-reviewer.md").write_text("reviewer v2\n", encoding="utf-8")
        (repo / "agents" / "overlord.md").write_text("overlord v2\n", encoding="utf-8")
        (repo / "overlord-policy.md").write_text("policy v2\n", encoding="utf-8")
        (repo / "app").mkdir()
        (repo / "app" / "pipeline_mcp_server.py").write_text("# server\n", encoding="utf-8")
    _git(["init", "-q"], cwd=repo)
    _git(["add", "-A"], cwd=repo)
    _git(
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        cwd=repo,
    )
    return repo


def _env(tmp_path: Path, remote: Path, with_claude_cli: bool = True, **extra: str) -> dict[str, str]:
    """Isolated HOME; PATH holds git, the system tools, and (optionally) the stub claude."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "git").symlink_to(GIT)
    if with_claude_cli:
        stub = bin_dir / "claude"
        stub.write_text(STUB_CLAUDE, encoding="utf-8")
        stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FAGAN_", "CLAUDE"))}
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}:/usr/bin:/bin",
        FAGAN_REPO_URL=str(remote),
        FAGAN_INSTALL_DIR=str(tmp_path / "install"),
        CLAUDE_STUB_LOG=str(tmp_path / "claude.log"),
    )
    env.update(extra)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _claude_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "claude.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --------------------------------------------------------------------------
# personas and policy
# --------------------------------------------------------------------------


def test_fresh_install_copies_every_persona_into_claude_agents(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path)))

    assert result.returncode == 0, result.stderr
    agents = tmp_path / "home" / ".claude" / "agents"
    assert (agents / "code-reviewer.md").read_text(encoding="utf-8") == "reviewer v2\n"
    assert (agents / "overlord.md").read_text(encoding="utf-8") == "overlord v2\n"


def test_fresh_install_copies_the_overlord_policy(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path)))

    assert result.returncode == 0, result.stderr
    policy = tmp_path / "home" / ".claude" / "overlord-policy.md"
    assert policy.read_text(encoding="utf-8") == "policy v2\n"


def test_existing_persona_is_kept_and_the_missing_one_still_installed(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    agents = tmp_path / "home" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-reviewer.md").write_text("my customized reviewer\n", encoding="utf-8")

    result = _run(_env(tmp_path, remote))

    assert result.returncode == 0, result.stderr
    assert (agents / "code-reviewer.md").read_text(encoding="utf-8") == "my customized reviewer\n"
    assert (agents / "overlord.md").read_text(encoding="utf-8") == "overlord v2\n"


def test_existing_overlord_policy_is_never_overwritten(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    claude_dir = tmp_path / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "overlord-policy.md").write_text("my policy\n", encoding="utf-8")

    result = _run(_env(tmp_path, remote))

    assert result.returncode == 0, result.stderr
    assert (claude_dir / "overlord-policy.md").read_text(encoding="utf-8") == "my policy\n"


def test_kept_persona_is_named_in_the_output(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    agents = tmp_path / "home" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-reviewer.md").write_text("mine\n", encoding="utf-8")

    result = _run(_env(tmp_path, remote))

    assert result.returncode == 0, result.stderr
    assert "code-reviewer.md" in result.stdout + result.stderr


# --------------------------------------------------------------------------
# MCP registration
# --------------------------------------------------------------------------


def test_registers_the_pipeline_server_with_the_venv_python(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path)))

    assert result.returncode == 0, result.stderr
    install = tmp_path / "install"
    expected = (
        f"mcp add -s user pipeline {install}/.venv/bin/python3 "
        f"{install}/app/pipeline_mcp_server.py"
    )
    assert expected in _claude_calls(tmp_path)


def test_already_registered_server_is_left_unchanged(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path), CLAUDE_GET_EXIT="0"))

    assert result.returncode == 0, result.stderr
    calls = _claude_calls(tmp_path)
    assert "mcp get pipeline" in calls
    assert not [c for c in calls if c.startswith("mcp add")]


def test_missing_claude_cli_still_succeeds_and_prints_the_manual_command(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path), with_claude_cli=False))

    assert result.returncode == 0, result.stderr
    assert "claude mcp add -s user pipeline" in result.stdout + result.stderr


def test_failed_registration_is_reported_on_stderr_without_failing_the_install(
    tmp_path: Path,
) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path), CLAUDE_ADD_EXIT="1"))

    assert result.returncode == 0, result.stdout
    assert "claude mcp add -s user pipeline" in result.stderr


def test_checkout_without_server_or_personas_skips_claude_setup(tmp_path: Path) -> None:
    result = _run(_env(tmp_path, _make_remote(tmp_path, with_claude_files=False)))

    assert result.returncode == 0, result.stderr
    assert _claude_calls(tmp_path) == []
    assert not (tmp_path / "home" / ".claude").exists()


def test_installer_never_calls_claude_mcp_remove(tmp_path: Path) -> None:
    _run(_env(tmp_path, _make_remote(tmp_path), CLAUDE_GET_EXIT="0"))

    assert not [c for c in _claude_calls(tmp_path) if "remove" in c]


# --------------------------------------------------------------------------
# docs describe the new behavior
# --------------------------------------------------------------------------


def _one_line_install_block() -> str:
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index("### One-line install")
    end = lines.index("This gets the MCP server registered and a first plan running end-to-end.")
    return "\n".join(lines[start:end])


def test_readme_one_line_install_no_longer_sends_users_to_step_2() -> None:
    assert "continue from step 2" not in _one_line_install_block()


def test_readme_one_line_install_says_to_restart_claude_code() -> None:
    assert "restart claude code" in _one_line_install_block().lower()


def test_demo_line_count_claim_matches_the_script() -> None:
    match = re.search(r"it is (\d+) lines of bash", DEMO.read_text(encoding="utf-8"))

    assert match is not None, "docs/DEMO.md no longer states the script's length"
    actual = len(SCRIPT.read_text(encoding="utf-8").splitlines())
    assert int(match.group(1)) == actual
