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

import json
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


def test_dry_run_seeds_the_model_registry_and_keeps_it_on_a_rerun(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()

    first = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    registry = state_dir / "model_registry.json"
    assert registry.is_file(), "the installer must create the registry compose bind-mounts"
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert "providers" in document, "a seeded registry must be a real registry document"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o644, (
        "the registry is not a secret and uid 10001 must be able to read it"
    )

    sentinel = '{"providers": {}, "roles": {"dispatch": {"provider": "ollama", "model": "keep-me"}}}'
    registry.write_text(sentinel, encoding="utf-8")

    second = _run("--dry-run", "--non-interactive", "--state-dir", str(state_dir), home=home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert registry.read_text(encoding="utf-8") == sentinel, (
        "a re-run must never overwrite the operator's role assignments"
    )


def test_the_seeded_registry_is_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/standalone/model_registry.json" in ignored


# ---------------------------------------------------------------------------
# Notifications (SN-01): the installer writes $STATE_DIR/notification.json in
# the shape pipeline/notification_routing.py loads, keeping every secret value
# in 0600 files and only env var *names* in the config.
# ---------------------------------------------------------------------------

WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/SECRETWEBHOOK"
SMTP_PASSWORD = "smtp-password-XYZ"


def _notify_files(tmp_path: Path) -> tuple[Path, Path]:
    hook = tmp_path / "slack-webhook.txt"
    hook.write_text(WEBHOOK_URL, encoding="utf-8")
    password = tmp_path / "smtp-password.txt"
    password.write_text(SMTP_PASSWORD, encoding="utf-8")
    return hook, password


def _notify_flags(hook: Path, password: Path) -> tuple[str, ...]:
    return (
        "--notify-slack-webhook-file",
        str(hook),
        "--notify-email-host",
        "smtp.example.com",
        "--notify-email-port",
        "2525",
        "--notify-email-user",
        "notify@example.com",
        "--notify-email-from",
        "ops@example.com",
        "--notify-email-to",
        "oncall@example.com",
        "--notify-email-password-file",
        str(password),
    )


def _notify_config(state_dir: Path) -> tuple[str, dict[str, object]]:
    path = state_dir / "notification.json"
    assert path.is_file(), f"installer did not write {path}"
    raw = path.read_text(encoding="utf-8")
    return raw, json.loads(raw)


def test_install_writes_notification_config(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    hook, password = _notify_files(tmp_path)
    proc = _run(
        "--dry-run",
        "--non-interactive",
        *_notify_flags(hook, password),
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    raw, data = _notify_config(state_dir)
    assert data["version"] == 1
    instances = data["instances"]
    assert instances["slack"] == {
        "type": "slack",
        "webhook_url_env": "SLACK_WEBHOOK_URL",
    }
    email = instances["email"]
    assert email["type"] == "email"
    assert email["host"] == "smtp.example.com"
    assert email["port"] == 2525, "the port must be a JSON number, not a quoted string"
    assert email["user"] == "notify@example.com"
    assert email["from"] == "ops@example.com"
    assert email["to"] == ["oncall@example.com"]
    assert email["password_env"] == "PIPELINE_NOTIFY_EMAIL_PASSWORD"

    routes = data["routes"]
    assert len(routes) == 1, f"expected exactly one route, got {routes!r}"
    route = routes[0]
    assert route["name"] == "all"
    assert route["match"] == {"plan": "*"}
    deliver = route["deliver"]
    assert len(deliver) == 1
    assert deliver[0]["events"] == ["*"]
    assert set(deliver[0]["to"]) == {"email", "slack"}

    assert "SECRETWEBHOOK" not in raw, "the webhook URL must never reach the config"
    assert SMTP_PASSWORD not in raw, "the SMTP password must never reach the config"


def test_install_notification_secrets_are_private(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    hook, password = _notify_files(tmp_path)
    proc = _run(
        "--dry-run",
        "--non-interactive",
        *_notify_flags(hook, password),
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    secrets_dir = state_dir / "secrets"
    for name, expected in (
        ("slack_webhook_url", WEBHOOK_URL),
        ("notify_email_password", SMTP_PASSWORD),
    ):
        path = secrets_dir / name
        assert path.is_file(), f"missing secret file {path}"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{name} has mode {oct(mode)}, expected 0o600"
        assert path.read_text(encoding="utf-8") == expected

    config = state_dir / "notification.json"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    env_text = (state_dir / ".env").read_text(encoding="utf-8")
    out = proc.stdout + proc.stderr
    for value in (WEBHOOK_URL, SMTP_PASSWORD):
        assert value not in env_text, "a notification secret leaked into .env"
        assert value not in out, "a notification secret was printed"


def test_install_without_notification_flags_writes_empty_config(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    proc = _run(
        "--dry-run",
        "--non-interactive",
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, data = _notify_config(state_dir)
    assert data == {"version": 1, "instances": {}, "routes": []}


def test_install_rerun_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    hook, password = _notify_files(tmp_path)
    flags = _notify_flags(hook, password)

    first = _run(
        "--dry-run", "--non-interactive", *flags, state_dir=state_dir, home=tmp_path / "home"
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_raw, first_data = _notify_config(state_dir)

    second = _run(
        "--dry-run", "--non-interactive", *flags, state_dir=state_dir, home=tmp_path / "home"
    )
    assert second.returncode == 0, second.stdout + second.stderr
    second_raw, second_data = _notify_config(state_dir)

    assert second_data == first_data, "a re-run must keep the notification config"
    assert second_raw == first_raw
    secrets_dir = state_dir / "secrets"
    for name, expected in (
        ("slack_webhook_url", WEBHOOK_URL),
        ("notify_email_password", SMTP_PASSWORD),
    ):
        path = secrets_dir / name
        assert path.read_text(encoding="utf-8") == expected
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_install_fails_closed_on_partial_email(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    proc = _run(
        "--dry-run",
        "--non-interactive",
        "--notify-email-host",
        "smtp.example.com",
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode != 0, "a host without from/to must abort the install"
    out = proc.stdout + proc.stderr
    assert "notify-email-from" in out or "notify-email-to" in out, out


def test_install_interactive_shows_notification_prompts(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    # Answers in prompt order: the seven pre-existing prompts, then SMTP host,
    # port, user, from and to, then the hidden secrets (four API keys, the SMTP
    # password, the Slack webhook) as blanks.
    answers = """pipeline.example.internal
8443
admin

github.com
http://host.docker.internal:11434
33418
smtp.example.com
2525
notify@example.com
ops@example.com
oncall@example.com





"""
    env = dict(os.environ)
    env["HOME"] = str(home)
    for key in ("PUBLIC_HOSTNAME", "PUBLIC_PORT", "PIPELINE_IMAGE", "ANTHROPIC_API_KEY", "GH_TOKEN"):
        env.pop(key, None)
    proc = subprocess.run(
        ["bash", str(INSTALL), "--dry-run", "--state-dir", str(state_dir)],
        input=answers,
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert "SMTP host (blank to skip email)" in out
    assert "Notifications are optional" in out


def test_notification_json_escapes_the_from_address(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    hook, password = _notify_files(tmp_path)
    from_address = 'Ops "Oncall" <ops@example.com>'
    proc = _run(
        "--dry-run",
        "--non-interactive",
        "--notify-slack-webhook-file", str(hook),
        "--notify-email-host", "smtp.example.com",
        "--notify-email-from", from_address,
        "--notify-email-to", "oncall@example.com",
        "--notify-email-password-file", str(password),
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    _, data = _notify_config(state_dir)
    assert data["instances"]["email"]["from"] == from_address


def test_invalid_smtp_port_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    hook, password = _notify_files(tmp_path)
    proc = _run(
        "--dry-run",
        "--non-interactive",
        "--notify-slack-webhook-file", str(hook),
        "--notify-email-host", "smtp.example.com",
        "--notify-email-port", "not-a-port",
        "--notify-email-from", "ops@example.com",
        "--notify-email-to", "oncall@example.com",
        "--notify-email-password-file", str(password),
        state_dir=state_dir,
        home=tmp_path / "home",
    )
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "--notify-email-port" in out
    assert "not-a-port" in out


def test_install_hands_the_notification_config_to_the_container_uid() -> None:
    """The rendered config must be readable by uid 10001 inside the container.

    ``$STATE_DIR/notification.json`` is bind-mounted read-only as
    ``/etc/pipeline/notification.json``. A bind mount keeps the host owner and
    mode, and the file is written 0600 (it names the operator's mail host and
    recipients), so a config left owned by whoever ran the installer is
    unreadable to the container's uid 10001: ``load_notification_config`` raises
    and ``safe_load_notification_config`` degrades to "deliver nothing", i.e.
    notifications die silently. The installer therefore has to hand the file to
    the same uid as the secrets it already chowns -- and the non-root warning
    must name it, since only root can do that.
    """
    script = _script()

    root_branch_start = script.index('if [ "$(id -u)" -eq 0 ]; then')
    root_branch_end = script.index("\nelse\n", root_branch_start)
    root_branch = script[root_branch_start:root_branch_end]

    chown_config = [
        line.strip()
        for line in root_branch.splitlines()
        if line.strip().startswith("chown") and "$NOTIFY_CONFIG_FILE" in line
    ]
    assert chown_config, (
        "install.sh never chowns $NOTIFY_CONFIG_FILE inside the root branch: the "
        "pipeline container runs as uid 10001 and a bind mount keeps the host "
        "owner, so the 0600 notification config would be unreadable and every "
        "notification would be dropped"
    )
    assert any('"$SECRETS_UID:$SECRETS_UID"' in line for line in chown_config), (
        f"the notification config must go to uid $SECRETS_UID: {chown_config!r}"
    )

    warn_lines = [line for line in script.splitlines() if "warn " in line]
    assert any("$NOTIFY_CONFIG_FILE" in line for line in warn_lines), (
        "the non-root remediation warning must name $NOTIFY_CONFIG_FILE, not only "
        "$SECRETS_DIR: a failed stack then reports an unreadable config nobody was "
        "told to chown"
    )
