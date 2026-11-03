"""Contract tests for the plan-approval gate on an *existing* standalone install.

SAD-01 made a fresh standalone install turn the plan-approval gate
(``PIPELINE_PLAN_APPROVAL``, ``pipeline/plan_approval.py``, ADR 0007) ON.
Safety gates default on (ADR 0004), but a server that was installed *before*
that default already has plans in flight, and every one of them was ingested
while the gate was off, so it carries no ``approval`` block: turning the gate on
stalls the whole server with no in-band explanation. ``approve_plan`` cannot
rescue such a plan directly either -- ``_decide_plan`` compares the stored
digest (``None``) with the recomputed one and refuses with "plan content
changed; re-ingest before approving".

So SAD-02 makes the installer keep the gate OFF for a pre-existing ``.env``
that has no ``PIPELINE_PLAN_APPROVAL`` line, and say so loudly (ADR 0004: an
opt-out must be visible). Fresh installs stay ON; an explicitly present value
(any accepted value) is never flipped and never warns.

These are static and dry-run only: no Docker daemon, no network, no secrets.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "deploy" / "install.sh"
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY_STANDALONE.md"

APPROVAL_LINE_RE = re.compile(r"^PIPELINE_PLAN_APPROVAL=(.*)$", re.MULTILINE)
# The upgrade warning is a `warn` line, distinct from the per-run log line that
# reports the resolved value (which mentions PIPELINE_PLAN_APPROVAL on every
# run, including the ones that must stay silent).
UPGRADE_WARNING = "plan-approval gate was kept off"


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


def _approval_lines(env_file: Path) -> list[str]:
    return APPROVAL_LINE_RE.findall(env_file.read_text(encoding="utf-8"))


def _single_approval_value(env_file: Path) -> str:
    values = _approval_lines(env_file)
    assert len(values) == 1, (
        "the generated .env must carry exactly one PIPELINE_PLAN_APPROVAL line; "
        f"found {len(values)}: {values!r}"
    )
    return values[0]


def _seed_installed_state(home: Path, state_dir: Path) -> Path:
    """A first dry run, i.e. a server that installed before SAD-01 existed."""
    proc = _install(home, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    env_file = state_dir / ".env"
    assert _single_approval_value(env_file) == "1"
    return env_file


def _drop_approval_line(env_file: Path) -> None:
    kept = [
        line
        for line in env_file.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("PIPELINE_PLAN_APPROVAL=")
    ]
    env_file.write_text("".join(kept), encoding="utf-8")


def test_rerun_over_env_without_the_line_keeps_the_gate_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    env_file = _seed_installed_state(home, state_dir)
    _drop_approval_line(env_file)

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _single_approval_value(env_file) == "0", (
        "a pre-existing .env without the line predates the gate; flipping it on "
        "would stall every in-flight plan"
    )
    assert "PIPELINE_PLAN_APPROVAL" in proc.stderr
    assert "approve_plan" in proc.stderr, (
        "the warning must tell the operator how to get out of the disabled state"
    )
    assert UPGRADE_WARNING in proc.stderr


def test_second_rerun_is_idempotent_and_silent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    env_file = _seed_installed_state(home, state_dir)
    _drop_approval_line(env_file)
    assert _install(home, state_dir).returncode == 0
    assert _single_approval_value(env_file) == "0"

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _single_approval_value(env_file) == "0", (
        "the second run must not append a duplicate line or flip the value"
    )
    assert UPGRADE_WARNING not in proc.stderr, (
        "the warning is for the one-time upgrade; warning forever trains the "
        "operator to ignore it"
    )


def test_rerun_never_flips_an_explicit_on(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env_file = state_dir / ".env"
    env_file.write_text(
        "PUBLIC_HOSTNAME=pipeline.example.com\nPIPELINE_PLAN_APPROVAL=1\n",
        encoding="utf-8",
    )

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _single_approval_value(env_file) == "1", (
        "an explicit on is an operator decision; a re-run must keep it"
    )
    assert UPGRADE_WARNING not in proc.stderr


def test_fresh_install_still_enables_the_gate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"

    proc = _install(home, state_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _single_approval_value(state_dir / ".env") == "1", (
        "safety gates default on for a fresh install (ADR 0004)"
    )
    assert UPGRADE_WARNING not in proc.stderr


def test_deploy_doc_documents_the_upgrade_path() -> None:
    lines = DEPLOY_DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index("### Upgrading")
    end = lines.index("### Backup and restore", start)
    section = "\n".join(lines[start:end])

    assert section.strip(), "the Upgrading section must not be empty"
    for needle in (
        "PIPELINE_PLAN_APPROVAL=0",
        "ingest_plan",
        "approve_plan",
        "plan content changed; re-ingest before approving",
    ):
        assert needle in section, (
            f"docs/DEPLOY_STANDALONE.md §7 Upgrading must mention {needle!r}: "
            "`git pull` + `up -d` skips install.sh, so the gate turns on"
        )
