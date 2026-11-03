"""Contract tests for the standalone plan-approval default (SAD-01).

Standalone installs must get the plan-approval gate (``PIPELINE_PLAN_APPROVAL``,
``pipeline/plan_approval.py``, ADR 0007) ON: safety gates default on (ADR 0004).
Before this, an ingested plan on a standalone server dispatched immediately and
``approve_plan`` answered ``{"skipped": "approval_disabled"}``.

These are static and dry-run only: no Docker daemon, no network, no secrets.

The subtle part is the compose substitution form. ``${VAR-1}`` substitutes only
when the variable is *unset*, so an explicit empty ``PIPELINE_PLAN_APPROVAL=``
reaches the container as ``""`` and ``approval_enabled()`` reads it as OFF.
``${VAR:-1}`` would turn that same explicit empty back into ``"1"`` and silently
re-enable the gate, so the colon form is asserted absent.

The installer rewrites the generated ``.env`` on every run, so it must resolve
the value *before* the heredoc: an explicit empty (or ``0``/``off``) survives a
re-run, and an invalid value fails before any byte of ``.env`` changes.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "deploy" / "install.sh"
COMPOSE = REPO_ROOT / "deploy" / "standalone" / "compose.yaml"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "standalone" / ".env.example"

APPROVAL_LINE_RE = re.compile(r"^PIPELINE_PLAN_APPROVAL=(.*)$", re.MULTILINE)


def _run(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
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
        "PIPELINE_PLAN_APPROVAL",
    ):
        env.pop(key, None)
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=120,
    )


def _install(home: Path, state_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        "--dry-run",
        "--non-interactive",
        "--state-dir",
        str(state_dir),
        home=home,
    )


def _approval_line(env_file: Path) -> str:
    match = APPROVAL_LINE_RE.search(env_file.read_text(encoding="utf-8"))
    assert match, "PIPELINE_PLAN_APPROVAL missing from the generated .env"
    return match.group(1)


def test_compose_substitutes_the_dash_form_default() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "PIPELINE_PLAN_APPROVAL: ${PIPELINE_PLAN_APPROVAL-1}" in text, (
        "the standalone pipeline service must pass PIPELINE_PLAN_APPROVAL "
        "through with a default of 1"
    )


def test_compose_does_not_use_the_colon_form() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${PIPELINE_PLAN_APPROVAL:-" not in text, (
        "${VAR:-1} rewrites an explicit empty value to 1, so an operator who "
        "disabled the gate by setting it empty would silently get it back on"
    )


def test_env_example_documents_an_enabled_default() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert APPROVAL_LINE_RE.search(text) and re.search(
        r"^PIPELINE_PLAN_APPROVAL=1$", text, re.MULTILINE
    ), ".env.example must ship PIPELINE_PLAN_APPROVAL=1 uncommented"


def test_fresh_install_writes_one_and_explains_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _approval_line(state_dir / ".env") == "1"
    assert "approve_plan" in proc.stderr, (
        "the installer log must tell the operator what the enabled gate means"
    )


def _write_env_with_value(state_dir: Path, value: str) -> bytes:
    state_dir.mkdir(parents=True, exist_ok=True)
    env_file = state_dir / ".env"
    env_file.write_text(
        "PUBLIC_HOSTNAME=pipeline.example.com\n"
        f"PIPELINE_PLAN_APPROVAL={value}\n",
        encoding="utf-8",
    )
    return env_file.read_bytes()


def test_rerun_keeps_an_explicit_zero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    _write_env_with_value(state_dir, "0")

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _approval_line(state_dir / ".env") == "0"


def test_rerun_keeps_an_explicit_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    _write_env_with_value(state_dir, "off")

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _approval_line(state_dir / ".env") == "off"


def test_rerun_keeps_an_explicit_empty_value(tmp_path: Path) -> None:
    """An empty value is OFF (approval_enabled()); it must not become 1."""
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    _write_env_with_value(state_dir, "")

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _approval_line(state_dir / ".env") == ""


def test_invalid_value_fails_before_rewriting_the_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    before = _write_env_with_value(state_dir, "yes")

    proc = _install(home, state_dir)

    assert proc.returncode != 0, "an unusable value must abort the install"
    assert "PIPELINE_PLAN_APPROVAL" in proc.stderr
    assert (state_dir / ".env").read_bytes() == before, (
        "validation must happen before the .env heredoc; otherwise the run "
        "fails after corrupting the operator's file"
    )


def test_env_copied_from_the_shipped_example_enables_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _approval_line(state_dir / ".env") == "1"
