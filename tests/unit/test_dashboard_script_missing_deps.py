"""Preflight dependency check in scripts/dashboard.sh (start path only).

The bug this pins down: with only requirements.txt installed,
``scripts/dashboard.sh start`` used to die inside uvicorn's importer with a
bare ``ModuleNotFoundError: No module named 'fastapi'`` buried at the tail of
dashboard.log, while the console printed a reassuring "started, pid ...".
The fix under test is an ``ensure_dashboard_deps`` preflight that runs
``"$PYBIN" -c 'import fastapi, uvicorn'`` before anything is spawned and, on
failure, prints a single actionable ``ERROR:`` line to stderr naming the
remediation command ``pip install -r requirements-dashboard.txt`` and exits 1
— without creating a pid file or touching dashboard.log.

Hermeticity: every test builds a throwaway tmp copy of the repo root (no
.venv, so the script's PYBIN fallback resolves to a stub ``python3`` we put
first on PATH) and never invokes the developer's real venv or uninstalls
anything. The stub python's behavior for the import probe is the only knob.

These tests assert only the behavior this story adds — never the script's
total contents, line count, or a whole-file hash — because
scripts/dashboard.sh is edited by other stories over time.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_SH = REPO_ROOT / "scripts" / "dashboard.sh"

REMEDIATION = "requirements-dashboard.txt"
PID_FILE_RE = re.compile(r"^\.dashboard\.\d+\.pid$")

# The exact probe the implementation is required to run. The stub python
# keys its exit code off this argv, so a probe that drifts (different
# module names, different -c payload) fails the failing-stub tests and the
# passing-stub test fails in the other direction — either way the drift is
# caught, not silently tolerated.
PROBE_SNIPPET = "import fastapi, uvicorn"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_stub_python(bin_dir: Path, probe_exit_code: int) -> Path:
    """Write an executable stub ``python3`` that exits *probe_exit_code* when
    invoked as ``python3 -c '<anything mentioning the probe>'`` and 0 for any
    other invocation (so unrelated python uses in the script keep working)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # Stub python3: fails ONLY the fastapi/uvicorn import probe.
            for arg in "$@"; do
              case "$arg" in
                *fastapi*) exit {probe_exit_code} ;;
              esac
            done
            exit 0
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _make_isolated_repo(tmp_path: Path, probe_exit_code: int) -> Path:
    """Tmp copy of the repo root with no .venv and a stub python3 first on
    PATH, so dashboard.sh's PYBIN fallback resolves to the stub."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(REPO_ROOT / "scripts", repo / "scripts")
    # Guard against a stray real .venv leaking into the copy.
    assert not (repo / ".venv").exists()
    _write_stub_python(tmp_path / "bin", probe_exit_code)
    return repo


def _run_dashboard(
    repo: Path,
    tmp_path: Path,
    *args: str,
    port: int = 8123,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(tmp_path / "bin") + os.pathsep + env.get("PATH", "")
    env["DASHBOARD_PORT"] = str(port)
    env.pop("DASHBOARD_HOST", None)
    env.pop("DASHBOARD_RELOAD", None)
    return subprocess.run(
        ["bash", "scripts/dashboard.sh", *args],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _pid_files(repo: Path) -> list[str]:
    return [p.name for p in repo.iterdir() if PID_FILE_RE.match(p.name)]


def _assert_no_pid_file(repo: Path) -> None:
    assert _pid_files(repo) == [], (
        f"start must not create a pid file when the dependency check fails; "
        f"found {_pid_files(repo)}"
    )


def _assert_log_clean(repo: Path) -> None:
    log = repo / "dashboard.log"
    if log.exists():
        contents = log.read_text(encoding="utf-8", errors="replace")
        assert "ModuleNotFoundError" not in contents, (
            "the dependency failure must be reported by the preflight check, "
            "not left as a Python traceback in dashboard.log"
        )


# --------------------------------------------------------------------------
# static shape of the fix (robust to unrelated edits elsewhere in the file)
# --------------------------------------------------------------------------


def test_dashboard_sh_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(DASHBOARD_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ensure_dashboard_deps_function_is_defined() -> None:
    """The new preflight must exist as its own function named exactly
    ``ensure_dashboard_deps`` (sibling of ensure_python)."""
    source = DASHBOARD_SH.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*ensure_dashboard_deps\s*\(\)\s*\{", source), (
        "scripts/dashboard.sh must define an ensure_dashboard_deps() function"
    )


def test_ensure_dashboard_deps_sits_after_ensure_python() -> None:
    """Placement contract: the new function is defined immediately after the
    existing ensure_python definition (sibling, not a rewrite of cmd_start)."""
    source = DASHBOARD_SH.read_text(encoding="utf-8")
    ensure_python_at = source.index("ensure_python()")
    deps_at = source.index("ensure_dashboard_deps()")
    assert deps_at > ensure_python_at, (
        "ensure_dashboard_deps() must be defined after ensure_python()"
    )
    between = source[ensure_python_at + len("ensure_python()") : deps_at]
    # "Immediately after": nothing but the tail of ensure_python's own body
    # (its exit-1 line and closing brace) may separate the two definitions.
    assert re.fullmatch(r"\s*\n\s*echo[^\n]*\n\s*exit 1\n\}\n\s*\n", between), (
        f"ensure_dashboard_deps() must immediately follow ensure_python(); "
        f"found intervening text: {between!r}"
    )


def test_cmd_start_calls_deps_check_right_after_ensure_python() -> None:
    """cmd_start must call the preflight on the line immediately after its
    existing ensure_python call — before any pid/log work."""
    source = DASHBOARD_SH.read_text(encoding="utf-8")
    cmd_start_at = source.index("cmd_start()")
    window = source[cmd_start_at : cmd_start_at + 400]
    m = re.search(
        r"(?m)^\s*ensure_python\s*$\n\s*ensure_dashboard_deps\s*$", window
    )
    assert m, (
        "cmd_start must call ensure_dashboard_deps immediately after "
        f"ensure_python; cmd_start head is: {window[:200]!r}"
    )
    # ...and the call must precede every pid-file/log side effect in cmd_start.
    head = window[: m.end()]
    assert "PID_FILE" not in head and "LOG_FILE" not in head, (
        "the dependency check must run before any pid-file or log work"
    )


def test_deps_check_runs_the_required_probe() -> None:
    """The function must probe with: PYBIN -c 'import fastapi, uvicorn',
    discarding stdout and stderr."""
    source = DASHBOARD_SH.read_text(encoding="utf-8")
    deps_at = source.index("ensure_dashboard_deps()")
    body = source[deps_at : source.index("cmd_start()")]
    probe = re.search(
        r'"\$PYBIN"\s+-c\s+(["\'])import fastapi, uvicorn\1', body
    )
    assert probe, (
        "ensure_dashboard_deps must run \"$PYBIN\" -c 'import fastapi, uvicorn'; "
        f"function body is: {body!r}"
    )
    # stdout+stderr discarded: a redirect to /dev/null on the probe line.
    probe_line = body[body.rfind("\n", 0, probe.start()) + 1 : body.find("\n", probe.end())]
    assert "/dev/null" in probe_line, (
        f"the probe's stdout and stderr must be discarded; line is: {probe_line!r}"
    )


def test_deps_failure_message_names_remediation_and_exits_1() -> None:
    """On probe failure: one ERROR: line to stderr naming the remediation
    command, then exit 1 — same shape as ensure_python."""
    source = DASHBOARD_SH.read_text(encoding="utf-8")
    deps_at = source.index("ensure_dashboard_deps()")
    body = source[deps_at : source.index("cmd_start()")]
    assert REMEDIATION in body, (
        f"the failure message must name the exact remediation ({REMEDIATION})"
    )
    assert "pip install" in body, (
        "the failure message must give the pip install command"
    )
    # Message goes to stderr and the function exits non-zero.
    assert re.search(r">&\s*2|\b1>&2\b", body), (
        "the ERROR message must be printed to stderr (>&2)"
    )
    assert re.search(r"(?m)^\s*exit 1\s*$", body), (
        "ensure_dashboard_deps must exit 1 when the probe fails"
    )
    # Same message shape as ensure_python: a single ERROR: line.
    assert re.search(r"ERROR: ", body), (
        "the failure message must follow the existing 'ERROR: ' shape"
    )


# --------------------------------------------------------------------------
# behavior: probe FAILS (python cannot import fastapi/uvicorn)
# --------------------------------------------------------------------------


def test_start_fails_fast_when_fastapi_missing(tmp_path: Path) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "start")
    assert result.returncode != 0, (
        "start must exit non-zero when fastapi/uvicorn cannot be imported; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_start_failure_message_names_remediation_on_stderr(
    tmp_path: Path,
) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "start")
    assert REMEDIATION in result.stderr, (
        "stderr must name the remediation file requirements-dashboard.txt; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ERROR: " in result.stderr, (
        "the failure must be reported as a single ERROR: line on stderr"
    )
    # One actionable line, not a traceback dump.
    error_lines = [ln for ln in result.stderr.splitlines() if "ERROR: " in ln]
    assert len(error_lines) == 1, (
        f"exactly one ERROR: line expected, got {error_lines!r}"
    )


def test_start_failure_creates_no_pid_file(tmp_path: Path) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "start")
    assert result.returncode != 0
    _assert_no_pid_file(repo)


def test_start_failure_writes_no_traceback_to_log(tmp_path: Path) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "start")
    assert result.returncode != 0
    _assert_log_clean(repo)


def test_start_failure_never_reaches_uvicorn_spawn(tmp_path: Path) -> None:
    """The stub python must have been invoked for the probe and the run must
    end at the preflight — no 'started' line, no uvicorn spawn attempt."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "start")
    assert result.returncode != 0
    assert "started" not in result.stdout, (
        "start must not report success when the dependency check fails"
    )


# --------------------------------------------------------------------------
# behavior: probe SUCCEEDS (deps importable) — check must not block start
# --------------------------------------------------------------------------


def test_start_proceeds_past_check_when_probe_succeeds(tmp_path: Path) -> None:
    """With a healthy python, the preflight must be transparent: start goes on
    to do its normal work (it may then fail for an unrelated reason, e.g. the
    stub can't actually run uvicorn) — the requirements-dashboard.txt error
    must NOT be printed."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=0)
    result = _run_dashboard(repo, tmp_path, "start")
    assert REMEDIATION not in result.stderr, (
        "the dependency error must not fire when fastapi/uvicorn import fine; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ERROR: " not in result.stderr, (
        f"no dependency ERROR expected on the happy path; stderr={result.stderr!r}"
    )


def test_start_happy_path_still_reports_started(tmp_path: Path) -> None:
    """Boundary: the check must not have broken the normal start flow — the
    script still prints its 'started' line (the stub python exits 0 for
    everything, so the spawn helper 'succeeds' trivially)."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=0)
    result = _run_dashboard(repo, tmp_path, "start")
    assert "started" in result.stdout, (
        f"normal start flow must be preserved; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------
# boundary: stop / status must NOT run the dependency probe
# --------------------------------------------------------------------------


def test_stop_works_with_failing_stub(tmp_path: Path) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "stop")
    assert result.returncode == 0, (
        f"stop must not run the dependency probe; stderr={result.stderr!r}"
    )
    assert "not running" in result.stdout
    assert REMEDIATION not in result.stderr


def test_status_reports_not_running_with_failing_stub(tmp_path: Path) -> None:
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "status")
    assert "not running" in result.stdout
    assert REMEDIATION not in result.stderr
    # cmd_status returns 1 when the dashboard is down — pre-existing behavior
    # this story must not change.
    assert result.returncode == 1


def test_restart_still_runs_the_check(tmp_path: Path) -> None:
    """restart = stop + start, so the start leg must still preflight."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    result = _run_dashboard(repo, tmp_path, "restart")
    assert result.returncode != 0, (
        "restart's start leg must fail fast when deps are missing"
    )
    assert REMEDIATION in result.stderr


# --------------------------------------------------------------------------
# hermeticity guard: the tests themselves never touch the real environment
# --------------------------------------------------------------------------


def test_tests_do_not_invoke_the_real_venv(tmp_path: Path) -> None:
    """Sanity check on the harness itself: the isolated repo has no .venv, so
    PYBIN can only resolve to the stub on PATH."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    assert not (repo / ".venv").exists()
    stub = tmp_path / "bin" / "python3"
    assert stub.exists() and os.access(stub, os.X_OK)
    probe = subprocess.run(
        [str(stub), "-c", "import fastapi, uvicorn"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 1


def test_no_real_environment_uninstall(tmp_path: Path) -> None:
    """The harness must never mutate the developer's environment: fastapi and
    uvicorn must still be importable by the real interpreter after a run (the
    failing-stub tests simulate a broken env with a stub, not by uninstalling)."""
    repo = _make_isolated_repo(tmp_path, probe_exit_code=1)
    _run_dashboard(repo, tmp_path, "start")
    for mod in ("fastapi", "uvicorn"):
        try:
            __import__(mod)
        except ImportError:  # pragma: no cover - depends on dev env
            pytest.skip(f"{mod} not installed in this environment; nothing to protect")