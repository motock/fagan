"""Agent spawn seam for the pipeline drivers.

Single place where the pipeline spawns agent harnesses. Today every spawn
site is a local Popen with stdout/stderr redirected to a log file; this
module preserves that identity exactly while adding a fail-closed
execution-mode gate (PIPELINE_EXEC_{ROLE}) so a later story can introduce
remote execution without touching the call sites again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


def _remap_path_prefix(value: str, local_prefix: str, remote_prefix: str) -> str:
    """Return value with local_prefix replaced by remote_prefix if it starts with local_prefix.

    Parameters
    ----------
    value: str
        The path or string to potentially remap.
    local_prefix: str
        Prefix that indicates the value is rooted in the local worktree.
    remote_prefix: str
        Prefix to use when remapping.

    Returns
    -------
    str
        The remapped value if a prefix match, otherwise the original value.
    """
    if value.startswith(local_prefix):
        return remote_prefix + value[len(local_prefix):]
    return value


def _build_spec(cmd, env, cwd, remote_cwd) -> dict:
    """Build the spec dictionary for remote execution.

    The spec contains the command with any local worktree paths remapped to the
    remote worktree, the environment with the same remapping applied, and the
    remote working directory.
    """
    remapped_cmd = [_remap_path_prefix(el, str(cwd), remote_cwd) for el in cmd]
    remapped_env = None
    if env is not None:
        remapped_env = {
            k: _remap_path_prefix(v, str(cwd), remote_cwd) for k, v in env.items()
        }
    return {
        "cmd": remapped_cmd,
        "env": remapped_env,
        "remote_cwd": remote_cwd,
        "branch": None,  # placeholder, will be set in _spawn_ssh
    }


def _spawn_ssh(cmd: list[str], *, cwd: Path, log_path: Path, append: bool, env: dict | None = None) -> AgentHandle:
    """Spawn a harness on a remote host via SSH.

    Raises ValueError if required environment variables are missing.
    """
    host = os.getenv("PIPELINE_REMOTE_EXEC_HOST", "").strip()
    sync_root = os.getenv("PIPELINE_REMOTE_SYNC_ROOT", "").strip()
    if not host or not sync_root:
        raise ValueError("PIPELINE_REMOTE_EXEC_HOST and PIPELINE_REMOTE_SYNC_ROOT must be set")
        # Note: this fallback is only for test environments; production should enforce env vars

    remote_url = f"ssh://{host}{sync_root}/repo-bare.git"
    remote_cwd = f"{sync_root}/worktrees/{cwd.name}"

    # Determine branch
    branch = subprocess.check_output(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    spec = _build_spec(cmd, env, cwd, remote_cwd)
    spec["branch"] = branch

    fd, spec_path = tempfile.mkstemp(suffix=".json")
    spec_file = os.fdopen(fd, "w")
    try:
        json.dump(spec, spec_file)
    except BaseException:
        # The reader never runs on a failed write, so the writer owns cleanup:
        # a partial spec already holds whichever env keys were serialized
        # (potentially credentials), and must not outlive this call.
        try:
            os.unlink(spec_path)
        except OSError:
            pass
        raise
    finally:
        spec_file.close()

    with open(log_path, "a" if append else "w") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pipeline.remote_exec",
                "--worktree",
                str(cwd),
                "--remote-url",
                remote_url,
                "--host",
                host,
                "--spec-file",
                spec_path,
            ],
            stdout=log_file,
            stderr=log_file,
        )
    return AgentHandle(pid=proc.pid, model="")





from app.backend_types import AgentHandle
from pipeline.sandbox import (
    build_docker_command,
    docker_binary_available,
    resolve_sandbox,
)

_VALID_MODES = ("local", "ssh")


def resolve_execution_mode(role: str = "dispatch") -> str:
    """Resolve the execution mode for ``role`` from PIPELINE_EXEC_{ROLE}.

    Defaults to ``"local"`` when the variable is unset or empty. Any other
    value raises ValueError naming the variable, the offending value, and the
    valid choices — fail closed, never a silent fallback.
    """
    var = f"PIPELINE_EXEC_{role.upper()}"
    raw = os.environ.get(var, "").strip().lower()
    if not raw:
        return "local"
    if raw not in _VALID_MODES:
        raise ValueError(
            f"{var}={raw!r} is not a valid execution mode; "
            f"expected one of: {', '.join(_VALID_MODES)}"
        )
    return raw


def spawn_local(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    append: bool,
    env: dict | None = None,
) -> AgentHandle:
    """Spawn ``cmd`` locally, streaming stdout/stderr into ``log_path``.

    Behavior-identical to the existing driver spawn sites: the child inherits
    the log file descriptor, so writes continue after this function returns.
    ``model`` stays empty here -- the drivers set it after spawn, as today.

    Also writes a per-line ISO-8601 UTC timestamp sidecar at
    ``str(log_path) + ".ts"`` (one line per agent.log line, same
    append/truncate semantics as log_path) via a background daemon
    thread draining the child's merged stdout+stderr. agent.log's own
    bytes are unaffected -- only the write mechanism changed from a
    raw fd redirect to a line-by-line relay, so every existing
    consumer that scans agent.log's raw content (STEP_CAP_MARKERS,
    INFRA_FAILURE_LOG_SUBSTRING, rebrief.py's anchored regexes,
    wedge_io.py's mtime check) is unaffected. Note: this does mean
    child output is now decoded as UTF-8 (errors="replace") rather
    than passed through as raw bytes -- a deliberate, narrow trade-off
    (agent CLIs emit UTF-8 text) required to timestamp lines at all.
    """
    mode = "a" if append else "w"
    log_file = open(log_path, mode, encoding="utf-8", errors="replace")
    ts_path = log_path.with_name(log_path.name + ".ts")
    ts_file = open(ts_path, mode, encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def _drain() -> None:
        try:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                ts_file.write(datetime.now(timezone.utc).isoformat() + "\n")
                ts_file.flush()
        finally:
            proc.stdout.close()
            log_file.close()
            ts_file.close()

    threading.Thread(target=_drain, daemon=True).start()
    return AgentHandle(pid=proc.pid, model="")


def spawn_harness(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    append: bool,
    env: dict | None = None,
    role: str = "dispatch",
) -> AgentHandle:
    """Spawn an agent harness for ``role`` using its configured execution mode.

    Resolves PIPELINE_EXEC_{ROLE} first; unknown modes raise before any
    process is spawned or log file is created (fail closed). ssh mode
    dispatches the remote driver directly and deliberately bypasses local
    docker-sandbox resolution — the harness runs on the ssh host, so no
    sandbox image or pinning applies.
    """
    mode = resolve_execution_mode(role)
    if mode == "ssh":
        return _spawn_ssh(cmd, cwd=cwd, log_path=log_path, append=append, env=env)
    sandbox = resolve_sandbox()
    if sandbox == "docker":
        if not docker_binary_available():
            raise RuntimeError(
                "PIPELINE_SANDBOX=docker is configured but the docker binary "
                "is not installed on this host: dispatch is REFUSED rather "
                "than falling back to unsandboxed execution"
            )
        allowlisted_env = (
            None
            if env is None
            else {
                key: value
                for key, value in env.items()
                if key.startswith(("LOCAL_AGENT_", "PIPELINE_"))
            }
        )
        cmd = build_docker_command(str(cwd), cmd, allowlisted_env)
    return spawn_local(cmd, cwd=cwd, log_path=log_path, append=append, env=env)