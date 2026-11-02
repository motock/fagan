"""Contract tests for ``deploy/install.sh`` (SA-16).

These are deliberately static and dry-run only: no Docker daemon, no network. The
contract they pin is the one an operator (or CI) relies on:

* the script parses under ``bash -n``;
* it never traces commands (a traced ``read -rs`` leaks a secret into the log);
* it sets ``umask 077`` before creating anything;
* secrets are read with ``read -rs``, never echoed, and never accepted from argv
  in non-interactive mode;
* ``--help`` documents the non-interactive switch;
* ``--dry-run --state-dir DIR`` writes every secret file the compose file lists
  at mode 0600 and, run twice, does not rewrite their contents (a re-run must
  never silently rotate a secret).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "deploy" / "install.sh"
COMPOSE = REPO_ROOT / "deploy" / "standalone" / "compose.yaml"

# Secret file names the compose file mounts. The installer must create all of
# them (empty when unused) because `docker compose up` fails on a missing file.
COMPOSE_SECRET_RE = re.compile(r"file:\s*\./secrets/([A-Za-z0-9_.-]+)")


def _script() -> str:
    assert INSTALL.is_file(), f"missing installer: {INSTALL}"
    return INSTALL.read_text(encoding="utf-8")


def _compose_secret_names() -> list[str]:
    names = sorted(set(COMPOSE_SECRET_RE.findall(COMPOSE.read_text(encoding="utf-8"))))
    assert names, "compose file lists no secret files; test needs updating"
    return names


def _run(
    *args: str, home: Path, state_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Keep the dry run hermetic: no ambient operator overrides.
    for key in (
        "PIPELINE_HOSTNAME",
        "PUBLIC_HOSTNAME",
        "PUBLIC_PORT",
        "PIPELINE_IMAGE",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
    ):
        env.pop(key, None)
    argv = ["bash", str(INSTALL), *args]
    if state_dir is not None:
        argv += ["--state-dir", str(state_dir)]
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=120,
    )


def test_script_parses_under_bash_n(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["bash", "-n", str(INSTALL)],
        capture_output=True,
        check=False,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr


def test_script_sets_umask_077() -> None:
    assert re.search(r"^umask\s+077\s*$", _script(), re.MULTILINE), (
        "installer must set umask 077 so every secret file starts private"
    )


def test_script_reads_secrets_without_echoing() -> None:
    assert "read -rs" in _script(), "secrets must be read with `read -rs` (no echo)"


def test_script_does_not_enable_tracing() -> None:
    script = _script()
    assert "set -x" not in script, "`set -x` would print secrets; it must never be enabled"
    assert not re.search(r"^set\s+-[a-z]*x", script, re.MULTILINE)


def test_script_runs_under_strict_mode() -> None:
    script = _script()
    assert re.search(r"^set -euo pipefail$", script, re.MULTILINE), (
        "installer must run with `set -euo pipefail`"
    )


def test_help_exits_zero_and_documents_non_interactive(tmp_path: Path) -> None:
    proc = _run("--help", state_dir=tmp_path / "state", home=tmp_path / "home")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "--non-interactive" in out
    for flag in (
        "--hostname",
        "--port",
        "--admin-user",
        "--first-user",
        "--anthropic-key-file",
        "--claude-oauth-token-file",
        "--gh-token-file",
        "--ollama-endpoint",
        "--ollama-key-file",
        "--repo-hosts",
        "--cli-callback-port",
        "--dry-run",
        "--state-dir",
    ):
        assert flag in out, f"--help must document {flag}"


def test_dry_run_writes_secrets_0600_and_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()

    first = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    expected = _compose_secret_names()
    secrets_dir = state_dir / "secrets"
    assert secrets_dir.is_dir(), f"dry run did not create {secrets_dir}"
    missing = [name for name in expected if not (secrets_dir / name).is_file()]
    assert not missing, f"dry run did not create compose secret files: {missing}"

    for name in expected:
        mode = stat.S_IMODE((secrets_dir / name).stat().st_mode)
        assert mode == 0o600, f"{name} has mode {oct(mode)}, expected 0o600"

    env_file = state_dir / ".env"
    assert env_file.is_file(), "dry run must write the .env file it planned with"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    env_text = env_file.read_text(encoding="utf-8")
    assert "PUBLIC_HOSTNAME=" in env_text
    assert "KEYCLOAK_ADMIN_PASSWORD=" in env_text
    # Generated secrets must be real random material, not a placeholder.
    password = re.search(r"^KEYCLOAK_ADMIN_PASSWORD=(.*)$", env_text, re.MULTILINE)
    assert password and len(password.group(1)) >= 32
    assert "change-me" not in password.group(1)

    before = {name: (secrets_dir / name).read_bytes() for name in expected}
    before["__env__"] = env_file.read_bytes()

    second = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)
    assert second.returncode == 0, second.stdout + second.stderr

    after = {name: (secrets_dir / name).read_bytes() for name in expected}
    after["__env__"] = env_file.read_bytes()
    assert after == before, "re-running the installer must not regenerate existing secrets"

    for name in expected:
        mode = stat.S_IMODE((secrets_dir / name).stat().st_mode)
        assert mode == 0o600, f"{name} has mode {oct(mode)} after re-run, expected 0o600"


def test_dry_run_prints_planned_compose_command(tmp_path: Path) -> None:
    proc = _run(
        "--dry-run",
        "--non-interactive",
        "--state-dir",
        str(tmp_path / "state"),
        home=tmp_path / "home",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "docker compose -f deploy/standalone/compose.yaml up -d --build --wait" in proc.stdout


def test_dry_run_never_prints_secret_material(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    proc = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=tmp_path / "home")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    env_text = (state_dir / ".env").read_text(encoding="utf-8")
    for key in ("KEYCLOAK_ADMIN_PASSWORD", "PIPELINE_DASHBOARD_CLIENT_SECRET", "PIPELINE_MCP_CLIENT_SECRET"):
        match = re.search(rf"^{key}=(.*)$", env_text, re.MULTILINE)
        assert match, f"{key} missing from .env"
        value = match.group(1).strip()
        assert len(value) >= 32
        assert value not in out, f"{key} value was printed by the installer"


def test_inline_secret_is_rejected_and_never_echoed(tmp_path: Path) -> None:
    marker = "sk-ant-inline-secret-must-not-be-accepted"
    proc = _run(
        f"--anthropic-key={marker}",
        "--dry-run",
        "--non-interactive",
        "--state-dir",
        str(tmp_path / "state"),
        home=tmp_path / "home",
    )
    assert proc.returncode != 0, "inline secret flags must be rejected; use --*-key-file"
    out = proc.stdout + proc.stderr
    assert marker not in out, "the installer echoed a secret passed on argv"


def test_unknown_flag_exits_non_zero(tmp_path: Path) -> None:
    proc = _run(
        "--definitely-not-a-flag",
        "--dry-run",
        "--state-dir",
        str(tmp_path / "state"),
        home=tmp_path / "home",
    )
    assert proc.returncode != 0
    assert "unknown" in (proc.stdout + proc.stderr).lower()


@pytest.mark.parametrize(
    "flag",
    [
        "--anthropic-key-file",
        "--claude-oauth-token-file",
        "--gh-token-file",
        "--ollama-key-file",
    ],
)
def test_secret_inputs_are_file_flags_only(flag: str, tmp_path: Path) -> None:
    script = _script()
    assert flag in script
    # The bare, value-taking variant must not be handled anywhere: a case arm of
    # the form `--anthropic-key)` would mean argv-sourced secrets.
    bare = flag[: -len("-file")]
    assert not re.search(rf"{re.escape(bare)}\)", script), (
        f"{bare} must not be accepted inline; use {flag}"
    )
    # And the script refuses it explicitly rather than silently prompting.
    assert "is not accepted" in script


def test_existing_env_placeholder_is_refused_without_writing(tmp_path: Path) -> None:
    """A .env carrying the old example constants must not be reused or rewritten."""
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    state_dir.mkdir()
    env_file = state_dir / ".env"
    seeded = "KEYCLOAK_ADMIN_PASSWORD=change-me-keycloak-admin\n"
    env_file.write_text(seeded, encoding="utf-8")

    proc = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)

    assert proc.returncode != 0, "a published placeholder must abort the install"
    out = proc.stdout + proc.stderr
    assert "KEYCLOAK_ADMIN_PASSWORD" in out, "the refusal must name the variable"
    assert "change-me-keycloak-admin" not in out, "the refusal must not echo the value"
    assert env_file.read_text(encoding="utf-8") == seeded, "a refused run must write nothing"
    secrets_dir = state_dir / "secrets"
    assert not secrets_dir.exists() or list(secrets_dir.iterdir()) == [], (
        "a refused run must write no secret files"
    )


def test_existing_secret_file_placeholder_is_refused(tmp_path: Path) -> None:
    """A placeholder already sitting in secrets/ must not be mounted on re-run."""
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    secrets_dir = state_dir / "secrets"
    secrets_dir.mkdir(parents=True)
    secret_file = secrets_dir / "oidc_client_secret"
    seeded = "change-me-dashboard-client-secret"
    secret_file.write_text(seeded, encoding="utf-8")

    proc = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)

    assert proc.returncode != 0, "a published placeholder must abort the install"
    out = proc.stdout + proc.stderr
    assert "oidc_client_secret" in out, "the refusal must name the secret file"
    assert seeded not in out, "the refusal must not echo the value"
    assert secret_file.read_text(encoding="utf-8") == seeded


def test_env_copied_from_the_shipped_example_installs(tmp_path: Path) -> None:
    """The documented copy-the-example workflow must generate real credentials."""
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    state_dir.mkdir()
    example = REPO_ROOT / "deploy" / "standalone" / ".env.example"
    (state_dir / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    proc = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    env_text = (state_dir / ".env").read_text(encoding="utf-8")
    for key in (
        "KEYCLOAK_ADMIN_PASSWORD",
        "PIPELINE_DASHBOARD_CLIENT_SECRET",
        "PIPELINE_MCP_CLIENT_SECRET",
    ):
        match = re.search(rf"^{key}=(.*)$", env_text, re.MULTILINE)
        assert match, f"{key} missing from .env"
        value = match.group(1).strip()
        assert value, f"{key} stayed empty after a copy-the-example install"
        assert "change-me" not in value.lower()
