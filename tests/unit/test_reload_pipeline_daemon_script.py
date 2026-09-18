"""Tests for ``scripts/reload_pipeline_daemon.sh``.

The script is a small, re-runnable operator helper that reloads the *installed*
advance-scheduler launchd agent and then shows what the daemon ACTUALLY ended
up running with, so an operator can verify that a plist change reached
production.

Live gap this closes (2026-09-18): editing ``launchd/*.plist`` in the repo
changes nothing for the running daemon. The daemon reads the INSTALLED agent
(``~/Library/LaunchAgents/com.fagan.pipeline.advance-scheduler.plist``), which
is a hand-made copy and is never re-read from the repo. A change therefore has
no effect until the installed file is edited AND the agent is reloaded.

These tests are RED until the script lands (the implementation dispatch), which
is the intended state.

HARD SAFETY CONSTRAINT: these tests must NEVER touch the real launchd domain or
the real ``~/Library/LaunchAgents``. Every ``launchctl`` invocation goes through
a stub executable placed earlier on ``PATH`` inside the test, with an explicit
``env=`` passed to ``subprocess.run`` and ``HOME`` pointed at a ``tmp_path``.
``ps``/``pgrep`` are stubbed the same way so no real process is inspected.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "reload_pipeline_daemon.sh"

LABEL = "com.fagan.pipeline.advance-scheduler"
PLIST_REL = "Library/LaunchAgents/com.fagan.pipeline.advance-scheduler.plist"

# ``launchctl unload "$PLIST"`` / ``launchctl load "$PLIST"`` (also tolerating
# the ``${PLIST}`` spelling and optional quoting).
_UNLOAD_RE = re.compile(r'launchctl\s+unload\s+"?\$\{?PLIST\}?"?')
_LOAD_RE = re.compile(r'launchctl\s+load\s+"?\$\{?PLIST\}?"?')
# ``ps eww "$PID"`` or an equivalent env-dumping ``ps`` invocation.
_PS_ENV_RE = re.compile(r"\bps\b[^\n]*(eww|-wwE|-E)\b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _script_text() -> str:
    assert SCRIPT.exists(), f"scripts/reload_pipeline_daemon.sh missing: {SCRIPT}"
    return SCRIPT.read_text(encoding="utf-8")


def _require_script() -> Path:
    assert SCRIPT.exists(), f"scripts/reload_pipeline_daemon.sh missing: {SCRIPT}"
    return SCRIPT


def _install_fake_plist(home: Path) -> Path:
    plist = home / PLIST_REL
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n", encoding="utf-8")
    return plist


# --- PATH stubs ------------------------------------------------------------
# Each stub is a tiny bash script. They never touch the real launchd domain.

_LAUNCHCTL_STUB = r"""#!/bin/bash
# Test stub for launchctl: records argv, never touches the real launchd domain.
log="${STUB_LOG:?STUB_LOG must be set}"
printf '%s\n' "$*" >> "$log"
cmd="${1:-}"
case "$cmd" in
  unload)
    exit "${STUB_UNLOAD_RC:-0}"
    ;;
  load)
    exit "${STUB_LOAD_RC:-0}"
    ;;
  list)
    if [ -n "${STUB_PID:-}" ]; then
      if [ "$#" -ge 2 ]; then
        printf '{\n\t"PID" = %s;\n\t"Label" = "%s";\n}\n' "$STUB_PID" "$2"
      else
        printf 'PID\tStatus\tLabel\n'
        printf '%s\t0\t%s\n' "$STUB_PID" "com.fagan.pipeline.advance-scheduler"
      fi
    fi
    exit 0
    ;;
  print)
    if [ -n "${STUB_PID:-}" ]; then
      printf 'pid = %s\n' "$STUB_PID"
    fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""

_PS_STUB = r"""#!/bin/bash
# Test stub for ps: prints a fake daemon environment, but only for a numeric
# pid argument (so an unresolved/empty pid cannot masquerade as a live one).
pid=""
for arg in "$@"; do
  case "$arg" in
    -p[0-9]*) pid="${arg#-p}" ;;
    [0-9]*) pid="$arg" ;;
  esac
done
if [ -z "$pid" ]; then
  exit 1
fi
printf '%s\n' "PIPELINE_LOCAL_NUM_CTX=32768 PIPELINE_AUTO_TRIAGE=1 PIPELINE_MAX_CONCURRENT_AGENTS=4 PIPELINE_DISPATCH_TIMEOUT_SECONDS=3600"
exit 0
"""

_PGREP_STUB = r"""#!/bin/bash
# Test stub for pgrep: mirrors real pgrep's exit-1-on-no-match behaviour.
if [ -n "${STUB_PID:-}" ]; then
  printf '%s\n' "$STUB_PID"
  exit 0
fi
exit 1
"""


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    for name, body in (
        ("launchctl", _LAUNCHCTL_STUB),
        ("ps", _PS_STUB),
        ("pgrep", _PGREP_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run_script(
    *,
    home: Path,
    stub_bin: Path,
    log_path: Path,
    pid: str = "12345",
    unload_rc: int = 0,
    load_rc: int = 0,
) -> subprocess.CompletedProcess:
    """Run the script with an explicit env: stub PATH first, HOME in tmp_path."""
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(home)
    env["STUB_LOG"] = str(log_path)
    env["STUB_PID"] = pid
    env["STUB_UNLOAD_RC"] = str(unload_rc)
    env["STUB_LOAD_RC"] = str(load_rc)
    return subprocess.run(
        ["bash", str(_require_script())],
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _launchctl_verbs(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    verbs: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            verbs.append(parts[0])
    return verbs


# ---------------------------------------------------------------------------
# Static shape of the script
# ---------------------------------------------------------------------------

class TestScriptShape:
    def test_script_exists(self):
        _script_text()

    def test_script_is_executable(self):
        _require_script()
        mode = SCRIPT.stat().st_mode
        assert mode & 0o111, (
            f"{SCRIPT} must be executable (mode {oct(mode)}); chmod +x it"
        )
        assert os.access(SCRIPT, os.X_OK)

    def test_bash_syntax_check_passes(self):
        _require_script()
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        assert result.returncode == 0, (
            f"bash -n failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_shebang_is_bash(self):
        first = _script_text().splitlines()[0]
        assert first.startswith("#!"), "script must start with a shebang"
        assert "bash" in first, f"shebang must invoke bash, got: {first!r}"

    def test_uses_strict_mode(self):
        assert "set -euo pipefail" in _script_text(), (
            "script must use `set -euo pipefail`"
        )

    def test_no_new_dependencies(self):
        text = _script_text()
        for dep in ("brew install", "pip install", "npm install"):
            assert dep not in text, (
                f"script must be plain bash with no new dependencies; found {dep!r}"
            )


# ---------------------------------------------------------------------------
# Static: the installed plist path, label, and reload calls
# ---------------------------------------------------------------------------

class TestInstalledPlistTarget:
    def test_names_installed_plist_path(self):
        text = _script_text()
        assert PLIST_REL in text, (
            "script must target the INSTALLED agent under "
            "~/Library/LaunchAgents/"
        )

    def test_plist_is_derived_from_home(self):
        text = _script_text()
        assert "HOME" in text, "PLIST must be derived from ${HOME}"

    def test_names_launchd_label(self):
        assert LABEL in _script_text(), (
            f"script must name the launchd label {LABEL}"
        )

    def test_does_not_fall_back_to_repo_copy(self):
        text = _script_text()
        # The PLIST target must never be assigned a repo-relative launchd/ path
        # (a comment naming the repo copy as a warning is fine).
        offenders = [
            line
            for line in text.splitlines()
            if re.match(r"\s*PLIST\s*=", line) and "launchd/" in line
        ]
        assert not offenders, (
            "PLIST must not be assigned the repo copy of the plist; found "
            f"{offenders}"
        )

    def test_calls_launchctl_unload_on_plist(self):
        assert _UNLOAD_RE.search(_script_text()), (
            'script must call `launchctl unload "$PLIST"`'
        )

    def test_calls_launchctl_load_on_plist(self):
        assert _LOAD_RE.search(_script_text()), (
            'script must call `launchctl load "$PLIST"`'
        )


# ---------------------------------------------------------------------------
# Static: the verify step (dump the daemon's real environment)
# ---------------------------------------------------------------------------

class TestVerifyStep:
    def test_dumps_daemon_env_with_ps(self):
        text = _script_text()
        assert _PS_ENV_RE.search(text), (
            'script must dump the running daemon environment, e.g. `ps eww "$PID"`'
        )

    def test_uses_a_resolved_pid_variable(self):
        assert "PID" in _script_text(), (
            "script must resolve the daemon pid into a PID variable"
        )

    def test_filters_to_pipeline_variables(self):
        assert "PIPELINE_" in _script_text(), (
            "script must filter the dumped environment to the PIPELINE_ variables"
        )

    def test_resolves_pid_from_the_label(self):
        text = _script_text()
        assert ("launchctl list" in text) or ("launchctl print" in text), (
            "script must resolve the daemon pid from the launchd label"
        )


# ---------------------------------------------------------------------------
# Static: the drift warning in the header
# ---------------------------------------------------------------------------

class TestDriftWarningComment:
    def test_header_warns_about_drift(self):
        assert "drift" in _script_text().lower(), (
            "script header must warn that the installed agent can drift from "
            "the repo copy"
        )

    def test_header_warns_against_wholesale_regenerate(self):
        text = _script_text().lower()
        assert "template" in text, (
            "script header must name the launchd/*.plist.template as a template"
        )
        assert "diff" in text, (
            "script header must say to diff the installed file before "
            "regenerating from the template"
        )


# ---------------------------------------------------------------------------
# Runtime: missing plist fails loudly and never touches launchd
# ---------------------------------------------------------------------------

class TestMissingPlist:
    def test_missing_plist_exits_nonzero(self, tmp_path, stub_bin):
        log = tmp_path / "launchctl.log"
        result = _run_script(home=tmp_path, stub_bin=stub_bin, log_path=log)
        assert result.returncode != 0, (
            "script must exit non-zero when the installed plist is missing"
        )

    def test_missing_plist_diagnostic_names_the_path(self, tmp_path, stub_bin):
        log = tmp_path / "launchctl.log"
        result = _run_script(home=tmp_path, stub_bin=stub_bin, log_path=log)
        expected = str(tmp_path / PLIST_REL)
        assert expected in result.stderr, (
            f"stderr must name the missing path {expected!r}; got: "
            f"{result.stderr!r}"
        )

    def test_missing_plist_is_not_created(self, tmp_path, stub_bin):
        log = tmp_path / "launchctl.log"
        _run_script(home=tmp_path, stub_bin=stub_bin, log_path=log)
        assert not (tmp_path / PLIST_REL).exists(), (
            "script must not create the missing plist"
        )

    def test_missing_plist_never_calls_launchctl_unload_or_load(
        self, tmp_path, stub_bin
    ):
        log = tmp_path / "launchctl.log"
        _run_script(home=tmp_path, stub_bin=stub_bin, log_path=log)
        verbs = _launchctl_verbs(log)
        assert "unload" not in verbs, (
            f"script must not unload when the plist is missing; saw {verbs}"
        )
        assert "load" not in verbs, (
            f"script must not load when the plist is missing; saw {verbs}"
        )


# ---------------------------------------------------------------------------
# Runtime: reload semantics
# ---------------------------------------------------------------------------

class TestReloadSemantics:
    def test_unload_failure_is_tolerated(self, tmp_path, stub_bin):
        """An agent that is not currently loaded cannot be unloaded again."""
        _install_fake_plist(tmp_path)
        log = tmp_path / "launchctl.log"
        result = _run_script(
            home=tmp_path, stub_bin=stub_bin, log_path=log,
            pid="12345", unload_rc=1, load_rc=0,
        )
        assert result.returncode == 0, (
            "a failing `launchctl unload` (agent not loaded) must not abort "
            f"the script; stderr={result.stderr!r}"
        )
        verbs = _launchctl_verbs(log)
        assert "unload" in verbs and "load" in verbs, (
            f"script must still attempt unload then load; saw {verbs}"
        )

    def test_load_failure_is_not_swallowed(self, tmp_path, stub_bin):
        _install_fake_plist(tmp_path)
        log = tmp_path / "launchctl.log"
        result = _run_script(
            home=tmp_path, stub_bin=stub_bin, log_path=log,
            pid="12345", unload_rc=0, load_rc=1,
        )
        assert result.returncode != 0, (
            "a failing `launchctl load` must surface as a non-zero exit"
        )
        assert "load" in _launchctl_verbs(log), (
            "script must have attempted the load"
        )

    def test_happy_path_reloads_and_dumps_daemon_env(self, tmp_path, stub_bin):
        plist = _install_fake_plist(tmp_path)
        log = tmp_path / "launchctl.log"
        result = _run_script(
            home=tmp_path, stub_bin=stub_bin, log_path=log,
            pid="12345", unload_rc=0, load_rc=0,
        )
        assert result.returncode == 0, (
            f"happy path must exit 0; stderr={result.stderr!r}"
        )
        log_text = log.read_text(encoding="utf-8")
        assert "unload" in log_text and "load" in log_text, (
            f"script must unload then load; log={log_text!r}"
        )
        assert str(plist) in log_text, (
            f"script must act on the installed plist path; log={log_text!r}"
        )
        combined = result.stdout + result.stderr
        assert "PIPELINE_" in combined, (
            "script must dump the running daemon's PIPELINE_ environment after "
            f"the load; got stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "PIPELINE_LOCAL_NUM_CTX" in combined, (
            "the dumped environment must be the daemon's real environment"
        )

    def test_no_pid_after_load_exits_nonzero(self, tmp_path, stub_bin):
        _install_fake_plist(tmp_path)
        log = tmp_path / "launchctl.log"
        result = _run_script(
            home=tmp_path, stub_bin=stub_bin, log_path=log,
            pid="", unload_rc=0, load_rc=0,
        )
        assert result.returncode != 0, (
            "script must exit non-zero when no daemon pid is running after "
            "the load"
        )
        combined = result.stdout + result.stderr
        assert re.search(r"pid|running|process", combined, re.IGNORECASE), (
            "script must print a clear message when no pid is running; got "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
