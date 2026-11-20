"""The entrypoint refuses a shared-secret dashboard on a non-loopback bind.

The dashboard index page hands the shared-secret API key to whoever requests
``/``. With ``PIPELINE_IDENTITY_PROVIDER=shared-secret`` and a dashboard that
listens beyond loopback, the key is therefore served to the network. These
tests run the real ``deploy/entrypoint.sh`` under ``sh`` (dash in the image)
with a hermetic environment and assert on whether the guard fires.

``PLAN_DIR`` and ``WORKTREE_ROOT`` point at paths that do not exist, so a run
that passes the guard stops at the later "state directory is missing" check
and never starts a child process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy" / "entrypoint.sh"

TIMEOUT_SECONDS = 60
GUARD_MESSAGE = "shared-secret dashboard"
STATE_MESSAGE = "state directory is missing"


def _environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """A minimal environment; nothing is inherited from the test runner."""
    secret = tmp_path / "oidc_client_secret"
    secret.write_text("test-client-secret\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PIPELINE_IDENTITY_PROVIDER": "shared-secret",
        "PIPELINE_OIDC_CLIENT_SECRET_FILE": str(secret),
        "PLAN_DIR": str(tmp_path / "missing-plans"),
        "WORKTREE_ROOT": str(tmp_path / "missing-worktrees"),
    }
    env.update(overrides)
    return env


def _run(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def test_shared_secret_on_all_interfaces_is_refused(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="0.0.0.0")

    proc = _run(env, tmp_path)

    assert proc.returncode != 0, proc.stderr
    assert GUARD_MESSAGE in proc.stderr, proc.stderr
    assert STATE_MESSAGE not in proc.stderr, "the guard must fire before the later checks"


def test_shared_secret_with_dashboard_host_unset_is_refused(tmp_path: Path) -> None:
    # The image default is 0.0.0.0; an unset DASHBOARD_HOST must not be treated
    # as loopback.
    env = _environment(tmp_path)

    proc = _run(env, tmp_path)

    assert proc.returncode != 0, proc.stderr
    assert GUARD_MESSAGE in proc.stderr, proc.stderr
    assert "DASHBOARD_HOST" in proc.stderr, proc.stderr


def test_shared_secret_on_a_routable_address_is_refused(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="10.0.0.5")

    proc = _run(env, tmp_path)

    assert proc.returncode != 0, proc.stderr
    assert GUARD_MESSAGE in proc.stderr, proc.stderr


def test_shared_secret_with_dashboard_ui_on_is_refused(tmp_path: Path) -> None:
    # Only the exact value `off` disables the UI and lifts the guard.
    env = _environment(tmp_path, DASHBOARD_HOST="0.0.0.0", PIPELINE_DASHBOARD_UI="on")

    proc = _run(env, tmp_path)

    assert proc.returncode != 0, proc.stderr
    assert GUARD_MESSAGE in proc.stderr, proc.stderr


def test_shared_secret_on_loopback_passes_the_guard(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="127.0.0.1")

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
    assert STATE_MESSAGE in proc.stderr, proc.stderr


def test_shared_secret_on_localhost_passes_the_guard(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="localhost")

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
    assert STATE_MESSAGE in proc.stderr, proc.stderr


def test_shared_secret_on_ipv6_loopback_passes_the_guard(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="::1")

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
    assert STATE_MESSAGE in proc.stderr, proc.stderr


def test_shared_secret_on_all_interfaces_passes_when_dashboard_ui_is_off(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="0.0.0.0", PIPELINE_DASHBOARD_UI="off")

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
    assert STATE_MESSAGE in proc.stderr, proc.stderr


def test_oidc_on_all_interfaces_passes_the_guard(tmp_path: Path) -> None:
    env = _environment(tmp_path, DASHBOARD_HOST="0.0.0.0", PIPELINE_IDENTITY_PROVIDER="oidc")

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
    assert STATE_MESSAGE in proc.stderr, proc.stderr


def test_guard_matches_the_exact_shared_secret_value_only(tmp_path: Path) -> None:
    # Other provider values are judged by the dashboard, not by this guard.
    env = _environment(
        tmp_path,
        DASHBOARD_HOST="0.0.0.0",
        PIPELINE_IDENTITY_PROVIDER="shared",
    )

    proc = _run(env, tmp_path)

    assert GUARD_MESSAGE not in proc.stderr, proc.stderr
