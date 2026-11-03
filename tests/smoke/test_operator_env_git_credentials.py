"""Dispatch-time git credentials come from the mounted forge token.

``deploy/lib/operator_env.sh`` exports ``PIPELINE_GIT_TOKEN_FILE``, but the git
processes dispatched work runs - ``pipeline/branch_guard.py``'s ``ls-remote``,
the worktree fetch, and the agent's own ``git push`` - are not children of the
clone path, so ``pipeline/repo_clone.py``'s temporary ``GIT_ASKPASS`` never
reaches them. Configured once in the shell library every child inherits, the
credential helper covers all of them.

The invariants these tests pin:

* a mounted, readable token configures a helper for each host in
  ``PIPELINE_REPO_URL_HOSTS`` and offers the token to no other host;
* the helper reads the token file at call time, so the secret never reaches
  ``.gitconfig``, an argv list, or a log line;
* an absent token file configures nothing at all rather than writing an empty
  helper;
* an entry in ``PIPELINE_REPO_URL_HOSTS`` that is not a plain hostname is
  skipped, not interpolated into a config key.

Like ``tests/smoke/test_entrypoint_secrets.py`` these tests source the shipped
library in a subprocess, so the behaviour under test is the shell code that
runs in the container rather than a description of it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

TIMEOUT_SECONDS = 60

TOKEN_VALUE = "ghp_test_token"

# Source the library the entrypoint sources, then run the caller's probe.
SOURCE_PREFIX = (
    ". deploy/lib/operator_env.sh; load_operator_credentials; ensure_repos_root; "
)


def _env(tmp_path: Path, token_path: Path) -> dict[str, str]:
    """A hermetic environment: no ambient credentials, no real /run/secrets."""
    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": str(tmp_path),
        "PIPELINE_IDENTITY_PROVIDER": "none-for-test",
        "PLAN_DIR": str(plans),
        "WORKTREE_ROOT": str(worktrees),
        "PIPELINE_REPOS_ROOT": str(tmp_path / "repos"),
        # Point every credential at a path that does not exist, so a stray
        # /run/secrets on the host can never leak into a test.
        "ANTHROPIC_API_KEY_FILE": str(tmp_path / "absent-anthropic"),
        "CLAUDE_CODE_OAUTH_TOKEN_FILE": str(tmp_path / "absent-oauth"),
        "GH_TOKEN_FILE": str(tmp_path / "absent-gh"),
        "OLLAMA_API_KEY_FILE": str(tmp_path / "absent-ollama"),
        "PIPELINE_GIT_TOKEN_FILE": str(token_path),
        "PIPELINE_REPO_URL_HOSTS": "github.com,gitlab.example",
    }


def _secret(path: Path, value: str = TOKEN_VALUE, mode: int = 0o600) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return path


def _run(probe: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # -eu mirrors the entrypoint's own mode, so an unset-variable mistake in the
    # library surfaces here as the failure it would be in the container.
    return subprocess.run(
        ["sh", "-eu", "-c", SOURCE_PREFIX + probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def test_mounted_token_configures_the_allowed_hosts(tmp_path: Path) -> None:
    env = _env(tmp_path, _secret(tmp_path / "token"))

    result = _run(
        "git config --global --get-regexp 'credential\\.https://.*\\.helper'", env
    )

    assert result.returncode == 0, result.stderr
    assert "credential.https://github.com.helper" in result.stdout
    assert "credential.https://gitlab.example.helper" in result.stdout
    # The helper references the token path; it never embeds the secret.
    assert TOKEN_VALUE not in result.stdout
    assert TOKEN_VALUE not in result.stderr


def test_helper_returns_the_token_to_git(tmp_path: Path) -> None:
    env = _env(tmp_path, _secret(tmp_path / "token"))

    result = _run(
        "printf 'protocol=https\\nhost=github.com\\n\\n'"
        " | GIT_TERMINAL_PROMPT=0 git credential fill",
        env,
    )

    assert result.returncode == 0, result.stderr
    assert "username=x-access-token" in result.stdout
    assert f"password={TOKEN_VALUE}" in result.stdout


def test_token_is_not_written_to_gitconfig(tmp_path: Path) -> None:
    env = _env(tmp_path, _secret(tmp_path / "token"))

    result = _run('cat "$HOME/.gitconfig"', env)

    assert result.returncode == 0, result.stderr
    # Written as a section header and a helper line, never as the secret value:
    # the helper re-reads the token file each time git asks for a password.
    assert '[credential "https://github.com"]' in result.stdout
    assert '$PIPELINE_GIT_TOKEN_FILE' in result.stdout
    assert TOKEN_VALUE not in result.stdout


def test_absent_token_file_configures_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path, tmp_path / "missing-token")

    result = _run("git config --global --get credential.https://github.com.helper", env)

    assert result.returncode != 0, result.stdout
    assert "credential.https://github.com.helper" not in result.stdout


def test_invalid_host_entry_is_skipped(tmp_path: Path) -> None:
    env = _env(tmp_path, _secret(tmp_path / "token"))
    env["PIPELINE_REPO_URL_HOSTS"] = "github.com,bad host"

    result = _run(
        "git config --global --get-regexp 'credential\\.https://.*\\.helper'", env
    )

    assert result.returncode == 0, result.stderr
    assert "credential.https://github.com.helper" in result.stdout
    assert "bad host" not in result.stdout
    assert f"password={TOKEN_VALUE}" not in result.stdout
