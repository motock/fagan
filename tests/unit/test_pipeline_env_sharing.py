"""Structural + behavioural wiring tests for the shared operator-env helper.

GOAL of the story: ONE operator env chain that BOTH long-running processes
read.  A new ``scripts/pipeline-env.sh`` helper sources
``$ROOT/.pipeline.env`` (if present) and THEN ``$ROOT/.dashboard.env`` (if
present), both under ``set -a`` allexport; ``scripts/dashboard.sh`` replaces
its inline ``.dashboard.env`` block with a single source of that helper, and
a new ``scripts/scheduler.sh`` (a start/stop/status wrapper for
``python -m pipeline.scheduler_daemon``) sources the same helper first.

House rules honoured here (mirroring test_install_script_wiring.py):

* the shell scripts are SHARED artifacts that later stories may extend, so
  structural tests assert MEMBERSHIP and ORDERING relative to fixed anchors
  only -- never total line counts, hashes, or exact file contents;
* dashboard.sh's survivor surface (PYBIN resolution, ensure_python,
  ensure_dashboard_deps, per-port PID_FILE naming, the detached os.setsid()
  spawn helper, cmd_stop's escalation, cmd_status, usage(), the case
  dispatch) is regression-pinned so the refactor cannot eat it;
* the only execution here is (a) ``bash -n`` syntax checks via subprocess
  and (b) sourcing the tiny helper in a throwaway temp ROOT -- no uvicorn,
  no daemon start, no venv creation, no network.

Helper contract assumed here (from the story brief): the helper is
*sourceable*, reads the repo root from the caller's ``ROOT`` variable
(dashboard.sh sets ``ROOT`` immediately before sourcing it), sources
``$ROOT/.pipeline.env`` before ``$ROOT/.dashboard.env``, each only when the
file exists, under ``set -a`` / ``set +a`` so sourced values are exported
and the caller's allexport state is restored afterwards.

RED state: until the implementation lands, pipeline-env.sh and scheduler.sh
do not exist and dashboard.sh still inlines its .dashboard.env block, so the
tests below fail on missing files / missing literals / missing gitignore
entries.  That is the intended TDD state, not a bug in this suite.

Intentionally NOT tested (would over-constrain or need a live process):
malformed env-file contents (today's inline block fails on those too, so
"identical behaviour" means propagating the failure -- unspecified), and
actually starting/stopping the dashboard or scheduler daemons.
"""

import re
import shlex
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_DASHBOARD_SH = _SCRIPTS / "dashboard.sh"
_SCHEDULER_SH = _SCRIPTS / "scheduler.sh"
_HELPER_SH = _SCRIPTS / "pipeline-env.sh"
_GITIGNORE = _REPO_ROOT / ".gitignore"
_PIPELINE_ENV_EXAMPLE = _REPO_ROOT / ".pipeline.env.example"
_DASHBOARD_ENV_EXAMPLE = _REPO_ROOT / ".dashboard.env.example"

_PIPELINE_ENV_NAME = ".pipeline.env"
_DASHBOARD_ENV_NAME = ".dashboard.env"
_HELPER_LITERAL = "scripts/pipeline-env.sh"
_DAEMON_MODULE = "pipeline.scheduler_daemon"

# dashboard.sh is a pre-existing dependency of the survivor regression
# tests; fail loudly (not skip) if it vanished.
if not _DASHBOARD_SH.exists():
    raise ImportError(
        f"TDD dependency missing: {_DASHBOARD_SH} does not exist. "
        "These wiring tests pin the survivor surface of that script."
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _source(path):
    return path.read_text(encoding="utf-8")


def _code_lines(path):
    """Non-comment lines (inline comments after code stay)."""
    return [
        ln
        for ln in _source(path).splitlines()
        if not ln.lstrip().startswith("#")
    ]


def _code(path):
    """Comment-stripped text, for offset/ordering assertions."""
    return "\n".join(_code_lines(path))


def _gitignore_lines():
    return [
        ln.strip()
        for ln in _GITIGNORE.read_text(encoding="utf-8").splitlines()
    ]


def _write_env_files(root, files):
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


def _source_helper_in(tmp_root, post_source):
    """Source the helper in a throwaway shell with ROOT=tmp_root.

    Mirrors dashboard.sh's real usage: ROOT is a plain (non-exported) shell
    variable set immediately before sourcing, and the caller runs under
    ``set -euo pipefail`` -- so the helper must be set -u safe too.
    """
    body = (
        "set -euo pipefail\n"
        f"ROOT={shlex.quote(str(tmp_root))}\n"
        f". {shlex.quote(str(_HELPER_SH))}\n"
        + post_source
    )
    return subprocess.run(
        ["bash", "-c", body],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _fail_msg(proc):
    return (
        f"sourcing the helper failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# --------------------------------------------------------------------------- #
# (a) scripts/pipeline-env.sh — the shared helper
# --------------------------------------------------------------------------- #
def test_helper_script_exists():
    assert _HELPER_SH.exists(), (
        "scripts/pipeline-env.sh is missing: the shared operator-env helper "
        "must exist so dashboard.sh and scheduler.sh source the same chain"
    )


def test_helper_references_both_env_files():
    code = _code(_HELPER_SH)
    assert _PIPELINE_ENV_NAME in code, (
        "helper does not reference .pipeline.env"
    )
    assert _DASHBOARD_ENV_NAME in code, (
        "helper does not reference .dashboard.env"
    )


def test_helper_sources_pipeline_env_before_dashboard_env():
    """Order of the two occurrences: .pipeline.env FIRST, .dashboard.env
    SECOND, so existing installs keep .dashboard.env's last-write
    precedence and nothing breaks for someone who already has one."""
    code = _code(_HELPER_SH)
    first_pipeline = code.find(_PIPELINE_ENV_NAME)
    first_dashboard = code.find(_DASHBOARD_ENV_NAME)
    assert first_pipeline != -1 and first_dashboard != -1
    assert first_pipeline < first_dashboard, (
        ".pipeline.env must be sourced BEFORE .dashboard.env (dashboard "
        "env is sourced second so existing installs keep their precedence)"
    )


def test_helper_sourcing_is_conditional_on_file_presence():
    """Each env file is sourced only *if present* (guarded by a file test)."""
    code_lines = _code_lines(_HELPER_SH)
    existence_tokens = ("-f", "-e", "-r")
    for name in (_PIPELINE_ENV_NAME, _DASHBOARD_ENV_NAME):
        guarded = [
            ln
            for ln in code_lines
            if name in ln and any(tok in ln for tok in existence_tokens)
        ]
        assert guarded, (
            f"helper must source {name} only when the file exists "
            f"(no code line guards {name} with a file-existence test)"
        )


def test_helper_wraps_sourcing_in_allexport():
    """set -a before the sourcing, set +a after it (both env files)."""
    code = _code(_HELPER_SH)
    assert "set -a" in code, (
        "helper must enable allexport (set -a) around the sourcing"
    )
    assert "set +a" in code, (
        "helper must restore allexport (set +a) after the sourcing"
    )
    first_set_a = code.find("set -a")
    last_set_plus_a = code.rfind("set +a")
    assert first_set_a < code.rfind(_PIPELINE_ENV_NAME), (
        "set -a must be in effect by the time .pipeline.env is sourced"
    )
    assert last_set_plus_a > code.rfind(_DASHBOARD_ENV_NAME), (
        "set +a must come after the .dashboard.env sourcing"
    )


def test_helper_honors_caller_provided_root():
    code = _code(_HELPER_SH)
    assert "$ROOT" in code, (
        "helper must read the repo root from the caller's ROOT variable "
        "(dashboard.sh sets ROOT immediately before sourcing the helper)"
    )


# --------------------------------------------------------------------------- #
# (b) scripts/dashboard.sh — rewire onto the helper, survivors intact
# --------------------------------------------------------------------------- #
def test_dashboard_sh_sources_shared_helper():
    code = _code(_DASHBOARD_SH)
    assert _HELPER_LITERAL in code, (
        "dashboard.sh must source scripts/pipeline-env.sh instead of "
        "inlining its own .dashboard.env block"
    )


_INLINE_DASHBOARD_ENV_RE = re.compile(
    r'(?:^|[\s;])\.\s+"\$ROOT/\.dashboard\.env"'
    r'|source\s+"\$ROOT/\.dashboard\.env"'
)


def test_dashboard_sh_no_longer_inlines_dashboard_env_block():
    code = _code(_DASHBOARD_SH)
    assert _INLINE_DASHBOARD_ENV_RE.search(code) is None, (
        "dashboard.sh still sources .dashboard.env directly; the inline "
        "block (~lines 48-52) must be replaced by the shared helper"
    )


# --- SURVIVOR REGRESSION: the refactor must not eat dashboard.sh's surface ---
def test_dashboard_sh_survivor_python_resolution_and_dep_checks():
    code = _code(_DASHBOARD_SH)
    assert "ensure_python" in code, "ensure_python survivor vanished"
    assert "ensure_dashboard_deps" in code, (
        "ensure_dashboard_deps survivor vanished"
    )


def test_dashboard_sh_survivor_per_port_pid_file_naming():
    pid_lines = [
        ln
        for ln in _code_lines(_DASHBOARD_SH)
        if "PID_FILE" in ln and "DASHBOARD_PORT" in ln
    ]
    assert pid_lines, (
        "per-port PID_FILE naming (.dashboard.<port>.pid) vanished from "
        "dashboard.sh"
    )


def test_dashboard_sh_survivor_detached_setsid_spawn():
    assert "os.setsid" in _code(_DASHBOARD_SH), (
        "the detached os.setsid() python -c spawn helper vanished"
    )


def test_dashboard_sh_default_port_unchanged():
    assert 'DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"' in _source(_DASHBOARD_SH), (
        "dashboard.sh's default port must stay 8000"
    )


# --------------------------------------------------------------------------- #
# (c) scripts/scheduler.sh — new start/stop/status wrapper
# --------------------------------------------------------------------------- #
def test_scheduler_sh_exists_and_is_executable():
    assert _SCHEDULER_SH.exists(), "scripts/scheduler.sh is missing"
    mode = _SCHEDULER_SH.stat().st_mode
    assert mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), (
        "scripts/scheduler.sh must be executable (chmod +x), like dashboard.sh"
    )


def test_scheduler_sh_sources_shared_helper():
    assert _SCHEDULER_SH.exists()
    code = _code(_SCHEDULER_SH)
    assert _HELPER_LITERAL in code, (
        "scheduler.sh must source scripts/pipeline-env.sh so both "
        "long-running processes read the same operator env chain"
    )


def test_scheduler_sh_sources_helper_before_daemon_launch():
    code = _code(_SCHEDULER_SH)
    helper_at = code.find("pipeline-env.sh")
    daemon_at = code.find(_DAEMON_MODULE)
    assert helper_at != -1 and daemon_at != -1
    assert helper_at < daemon_at, (
        "scheduler.sh must source the shared env helper before it launches "
        "pipeline.scheduler_daemon"
    )


def test_scheduler_sh_launches_scheduler_daemon_module():
    code = _code(_SCHEDULER_SH)
    assert _DAEMON_MODULE in code, (
        "scheduler.sh must wrap python -m pipeline.scheduler_daemon"
    )
    daemon_lines = [ln for ln in code.splitlines() if _DAEMON_MODULE in ln]
    assert any("-m" in ln for ln in daemon_lines), (
        "the daemon must be launched as a module (-m pipeline.scheduler_daemon)"
    )


def test_scheduler_sh_has_pid_alive_and_read_pid_helpers():
    code = _code(_SCHEDULER_SH)
    assert "pid_alive" in code, "scheduler.sh lacks a pid_alive helper"
    assert "read_pid" in code, "scheduler.sh lacks a read_pid helper"


def test_scheduler_sh_pid_and_log_files_live_in_repo_root():
    code_lines = _code_lines(_SCHEDULER_SH)
    pid_lines = [ln for ln in code_lines if "PID_FILE" in ln]
    assert pid_lines, "scheduler.sh must define a PID_FILE"
    assert any("$ROOT" in ln for ln in pid_lines), (
        "scheduler.sh pid file must live in the repo root ($ROOT/...)"
    )
    log_lines = [
        ln for ln in code_lines if "LOG_FILE" in ln or "scheduler.log" in ln
    ]
    assert log_lines, "scheduler.sh must define a log file"
    assert any("$ROOT" in ln for ln in log_lines), (
        "scheduler.sh log file must live in the repo root ($ROOT/...)"
    )


_SUBCOMMANDS = ("start", "stop", "restart", "status")


def test_scheduler_sh_subcommand_dispatch_shape():
    """Same dispatch shape as dashboard.sh: usage() + case over the four
    subcommands (membership only — the shared script may grow)."""
    code = _code(_SCHEDULER_SH)
    assert re.search(r"(?m)^\s*case\s+\S", code), "no case dispatch found"
    assert re.search(r"(?m)^\s*usage\s*\(\s*\)", code), "no usage() function"
    for sub in _SUBCOMMANDS:
        assert re.search(rf"(?m)^\s*{re.escape(sub)}\s*\)", code), (
            f"scheduler.sh dispatch is missing the '{sub})' case label"
        )


# --------------------------------------------------------------------------- #
# (d) .pipeline.env.example + .gitignore
# --------------------------------------------------------------------------- #
def test_pipeline_env_example_exists_and_documents_core_vars():
    assert _PIPELINE_ENV_EXAMPLE.exists(), (
        ".pipeline.env.example is missing (the committed copy-me template)"
    )
    text = _PIPELINE_ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in ("PLAN_DIR", "WORKTREE_ROOT", "PIPELINE_AUTONOMY"):
        assert var in text, f".pipeline.env.example does not document {var}"


_PROVIDER_ROUTING_VARS = (
    "PIPELINE_BACKEND_CHAT",
    "PIPELINE_BACKEND_DECOMPOSE",
    "PIPELINE_LOCAL_MODEL_DEFAULT",
    "PIPELINE_MODEL_REGISTRY_PATH",
)


def test_pipeline_env_example_documents_provider_routing_vars():
    assert _PIPELINE_ENV_EXAMPLE.exists()
    text = _PIPELINE_ENV_EXAMPLE.read_text(encoding="utf-8")
    matched = [v for v in _PROVIDER_ROUTING_VARS if v in text]
    assert matched, (
        ".pipeline.env.example documents none of the provider routing vars "
        f"(expected at least one of {_PROVIDER_ROUTING_VARS})"
    )


def test_gitignore_ignores_pipeline_env():
    assert _PIPELINE_ENV_NAME in _gitignore_lines(), (
        ".gitignore must contain a .pipeline.env entry (next to .dashboard.env)"
    )


def test_gitignore_still_ignores_dashboard_env():
    assert _DASHBOARD_ENV_NAME in _gitignore_lines(), (
        "the existing .dashboard.env gitignore entry must survive"
    )


def test_gitignore_does_not_ignore_pipeline_env_example():
    assert ".pipeline.env.example" not in _gitignore_lines(), (
        ".pipeline.env.example is the committed template and must stay tracked"
    )


def test_dashboard_env_example_still_exists():
    assert _DASHBOARD_ENV_EXAMPLE.exists(), (
        ".dashboard.env.example must not be deleted"
    )


# --------------------------------------------------------------------------- #
# (e) behavioural: sourcing the helper in a throwaway temp ROOT
# --------------------------------------------------------------------------- #
def test_helper_exports_vars_from_both_env_files(tmp_path):
    _write_env_files(
        tmp_path,
        {
            _PIPELINE_ENV_NAME: "PIPELINE_ENV_PROBE=from-pipeline\n",
            _DASHBOARD_ENV_NAME: "DASHBOARD_ENV_PROBE=from-dashboard\n",
        },
    )
    proc = _source_helper_in(
        tmp_path,
        'printf "pipeline_value=%s\\n" "${PIPELINE_ENV_PROBE:-<unset>}"\n'
        'printf "dashboard_value=%s\\n" "${DASHBOARD_ENV_PROBE:-<unset>}"\n'
        'printf "pipeline_exported=%s\\n" '
        '"$(bash -c \'printf %s "${PIPELINE_ENV_PROBE:-}"\')"\n'
        'printf "dashboard_exported=%s\\n" '
        '"$(bash -c \'printf %s "${DASHBOARD_ENV_PROBE:-}"\')"\n',
    )
    assert proc.returncode == 0, _fail_msg(proc)
    assert "pipeline_value=from-pipeline" in proc.stdout
    assert "dashboard_value=from-dashboard" in proc.stdout
    assert "pipeline_exported=from-pipeline" in proc.stdout, (
        "vars from .pipeline.env must be EXPORTED (set -a), not merely set"
    )
    assert "dashboard_exported=from-dashboard" in proc.stdout, (
        "vars from .dashboard.env must be EXPORTED (set -a), not merely set"
    )


def test_helper_dashboard_env_wins_on_conflicting_keys(tmp_path):
    _write_env_files(
        tmp_path,
        {
            _PIPELINE_ENV_NAME: "SHARED_PROBE=from-pipeline\n",
            _DASHBOARD_ENV_NAME: "SHARED_PROBE=from-dashboard\n",
        },
    )
    proc = _source_helper_in(
        tmp_path, 'printf "shared=%s\\n" "${SHARED_PROBE:-<unset>}"\n'
    )
    assert proc.returncode == 0, _fail_msg(proc)
    assert "shared=from-dashboard" in proc.stdout, (
        ".dashboard.env is sourced SECOND and must keep its current "
        "last-write precedence for existing installs"
    )


def test_helper_with_only_dashboard_env_behaves_as_today(tmp_path):
    _write_env_files(
        tmp_path, {_DASHBOARD_ENV_NAME: "DASHBOARD_ENV_PROBE=dashboard-only\n"}
    )
    proc = _source_helper_in(
        tmp_path,
        'printf "dashboard_value=%s\\n" "${DASHBOARD_ENV_PROBE:-<unset>}"\n'
        'printf "pipeline_value=%s\\n" "${PIPELINE_ENV_PROBE:-<unset>}"\n'
        'printf "dashboard_exported=%s\\n" '
        '"$(bash -c \'printf %s "${DASHBOARD_ENV_PROBE:-}"\')"\n',
    )
    assert proc.returncode == 0, _fail_msg(proc)
    assert "dashboard_value=dashboard-only" in proc.stdout
    assert "dashboard_exported=dashboard-only" in proc.stdout
    assert "pipeline_value=<unset>" in proc.stdout, (
        "a missing .pipeline.env must be skipped silently"
    )


def test_helper_with_no_env_files_is_a_noop(tmp_path):
    proc = _source_helper_in(
        tmp_path, 'printf "probe=%s\\n" "${PROBE:-<unset>}"\n'
    )
    assert proc.returncode == 0, _fail_msg(proc)
    assert "probe=<unset>" in proc.stdout


def test_helper_tolerates_empty_env_files(tmp_path):
    (tmp_path / _PIPELINE_ENV_NAME).write_text("", encoding="utf-8")
    (tmp_path / _DASHBOARD_ENV_NAME).write_text("", encoding="utf-8")
    proc = _source_helper_in(tmp_path, 'printf "done=%s\\n" ok\n')
    assert proc.returncode == 0, _fail_msg(proc)
    assert "done=ok" in proc.stdout


def test_helper_restores_allexport_state(tmp_path):
    _write_env_files(tmp_path, {_DASHBOARD_ENV_NAME: "DASHBOARD_ENV_PROBE=x\n"})
    proc = _source_helper_in(
        tmp_path,
        'if set +o | grep -Fq "set +o allexport"; then '
        "echo allexport-restored; else echo allexport-left-on; fi\n",
    )
    assert proc.returncode == 0, _fail_msg(proc)
    assert "allexport-restored" in proc.stdout, (
        "helper must pair set -a with set +a; leaking allexport would "
        "export every variable the caller sets afterwards"
    )


# --------------------------------------------------------------------------- #
# (f) negative: bash -n syntax gate for all three scripts
# --------------------------------------------------------------------------- #
def test_all_three_scripts_pass_bash_n():
    problems = []
    for path in (_HELPER_SH, _DASHBOARD_SH, _SCHEDULER_SH):
        proc = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            problems.append(
                f"{path}: rc={proc.returncode}: {proc.stderr.strip()}"
            )
    assert not problems, "bash -n reported problems:\n" + "\n".join(problems)