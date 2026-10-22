"""Read-only detection of the machine's guided-install state.

The guided installer has to know what is already true before it offers to
change anything: which model registry file is in force and what it says,
whether the launchd/systemd services are rendered and running, and whether
the venv, the MCP registration, the personas, and the overlord policy are in
place. ``pipeline.setup.detect`` answers those questions and writes nothing.

Every external call is injectable so tests never touch the real host:
``runner`` executes an argv and returns ``(returncode, stdout, stderr)``, and
``which`` locates an executable. Both have conservative stdlib defaults, and
every probe is bounded by a timeout. Any ``OSError``, ``TimeoutExpired``,
``UnicodeDecodeError``, or malformed result collapses to ``None``/``[]`` plus
a short error string: detection never raises and never hangs, because it has
to be able to report what is missing on a half-broken machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

__all__ = ["collect_state", "detect_registry", "detect_services"]

#: The service label the scheduler installs under on both daemons.
LABEL = "com.fagan.pipeline.advance-scheduler"

#: Injectables. ``runner`` executes an argv and returns
#: ``(returncode, stdout, stderr)``; ``which`` locates an executable.
Runner = Callable[..., tuple[int, str, str]]
Which = Callable[[str], "str | None"]

# Every probe is bounded; nothing in this module may hang the installer.
CHECKS_TIMEOUT = 60
IMPORTS_TIMEOUT = 30
MCP_TIMEOUT = 30
SERVICE_TIMEOUT = 10

_REGISTRY_ENV = "PIPELINE_MODEL_REGISTRY_PATH"
_LOCAL_REGISTRY = "model_registry.local.json"
_COMMITTED_REGISTRY = "model_registry.json"
_IMPORT_PROBE = "import mcp, httpx, fastapi, uvicorn"


def _default_runner(argv: Sequence[str], timeout: int | None = None):
    """Run ``argv`` and report ``(returncode, stdout, stderr)``.

    This is the only place the module starts a host process. It never raises
    and never hangs: a missing executable, a timeout, or undecodable output
    all resolve to ``None`` so callers can degrade to "unknown".
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def _probe(runner, argv, timeout):
    """Call ``runner`` for ``argv``, normalising every failure to ``None``."""
    try:
        result = runner(list(argv), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if not isinstance(result, (tuple, list)) or len(result) != 3:
        return None
    code, stdout, stderr = result
    if (
        not isinstance(code, int)
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
    ):
        return None
    return code, stdout, stderr


def _existing(directory, pattern):
    """Sorted absolute paths under ``directory`` matching ``pattern``."""
    try:
        return sorted(str(p) for p in directory.glob(pattern) if p.is_file())
    except OSError:
        return []


def _markdown_stems(directory):
    """The set of ``*.md`` file stems inside ``directory`` (empty if absent)."""
    try:
        return {p.stem for p in directory.glob("*.md") if p.is_file()}
    except OSError:
        return set()


def _run_checks(repo, runner):
    """The install-checks list, or ``([], reason)`` when it cannot be had."""
    script = repo / "scripts" / "install_checks.py"
    if not script.is_file():
        return [], "scripts/install_checks.py is missing; no checks JSON"
    probe = _probe(runner, [sys.executable, str(script), "--json"], CHECKS_TIMEOUT)
    if probe is None:
        return [], "install_checks.py probe failed; no checks JSON"
    code, stdout, _stderr = probe
    if code != 0:
        return [], f"install_checks.py exited with status {code}; no checks JSON"
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return [], "install_checks.py did not print JSON"
    if not isinstance(parsed, list):
        return [], "install_checks.py did not print a JSON list"
    return parsed, None


def _venv_python(repo):
    """The venv interpreter path, or ``None`` when it is not executable."""
    candidate = repo / ".venv" / "bin" / "python3"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _mcp_registered(runner, which):
    """Whether ``claude mcp get pipeline`` succeeds (None when unknown)."""
    try:
        claude = which("claude")
    except OSError:
        return None
    if not claude:
        return None
    probe = _probe(runner, ["claude", "mcp", "get", "pipeline"], MCP_TIMEOUT)
    if probe is None:
        return None
    return probe[0] == 0


def _personas(repo, home):
    """Repo personas split into those installed under ``home`` and not."""
    shipped = _markdown_stems(repo / "agents")
    installed = _markdown_stems(home / ".claude" / "agents")
    return {
        "installed": sorted(name for name in shipped if name in installed),
        "missing": sorted(name for name in shipped if name not in installed),
    }


def detect_registry(repo_root, *, environ: Mapping[str, str] | None = None) -> dict:
    """Resolve and parse the model registry without leaking its secrets.

    The path is chosen in priority order: ``PIPELINE_MODEL_REGISTRY_PATH`` when
    it is set and non-empty, then ``<repo>/model_registry.local.json`` when
    that file exists, then the committed ``<repo>/model_registry.json``. A
    missing file is a normal state (``exists=False``), not an error; malformed
    JSON yields empty ``roles``/``data`` and an error naming the path, never
    the file's contents.

    Returns ``{"path", "exists", "roles", "provider_count", "data", "error"}``
    where ``data`` is the parsed mapping so callers can reuse it without
    re-reading the file.
    """
    repo = Path(repo_root)
    env = os.environ if environ is None else environ
    override = env.get(_REGISTRY_ENV, "")
    if isinstance(override, str) and override:
        path = Path(override)
    else:
        local = repo / _LOCAL_REGISTRY
        path = local if local.exists() else repo / _COMMITTED_REGISTRY

    exists = path.exists()
    data: dict = {}
    error = None
    if exists:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
        else:
            error = f"malformed JSON: {path}"

    roles = data.get("roles")
    if not isinstance(roles, dict):
        roles = {}
    providers = data.get("providers")
    provider_count = len(providers) if isinstance(providers, dict) else 0

    return {
        "path": str(path),
        "exists": exists,
        "roles": roles,
        "provider_count": provider_count,
        "data": data,
        "error": error,
    }


def _launchctl_running(runner):
    """True when ``launchctl list`` shows the scheduler label as a whole field.

    The label must be the last whitespace-separated field of its own line: a
    sibling service whose label merely *contains* ours (``...-backup``) is a
    different service and must not read as running. A failed probe is ``None``.
    """
    probe = _probe(runner, ["launchctl", "list"], SERVICE_TIMEOUT)
    if probe is None:
        return None
    for line in probe[1].splitlines():
        fields = line.split()
        if fields and fields[-1] == LABEL:
            return True
    return False


def detect_services(
    repo_root,
    *,
    platform: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Report rendered and running state of the pipeline daemons.

    ``platform`` defaults to ``sys.platform``. On darwin the rendered services
    are the ``com.fagan.pipeline.*.plist`` files under ``<repo>/launchd`` and
    "running" comes from ``launchctl list`` mentioning the scheduler label. On
    linux they are the ``*.service`` files under ``<repo>/systemd`` and
    ``systemctl --user is-active`` decides. Any other platform reports nothing
    rendered and an unknown running state. Every probe is bounded by a
    timeout; a failed probe reports ``running=None`` rather than raising.
    """
    chosen = sys.platform if platform is None else platform
    repo = Path(repo_root)
    run = _default_runner if runner is None else runner

    if chosen == "darwin":
        rendered = _existing(repo / "launchd", "com.fagan.pipeline.*.plist")
        return {
            "platform": chosen,
            "rendered": rendered,
            "running": _launchctl_running(run),
        }

    if chosen == "linux":
        rendered = _existing(repo / "systemd", "*.service")
        probe = _probe(
            run,
            ["systemctl", "--user", "is-active", f"{LABEL}.service"],
            SERVICE_TIMEOUT,
        )
        running = None if probe is None else probe[0] == 0
        return {"platform": chosen, "rendered": rendered, "running": running}

    return {"platform": chosen, "rendered": [], "running": None}


def collect_state(
    repo_root,
    *,
    home: Path | None = None,
    runner: Runner | None = None,
    which: Which | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Snapshot everything the guided installer needs to know.

    Nothing here writes, and nothing raises: a missing script, venv, ``claude``,
    or home directory simply reports as empty/unknown so the installer can show
    the user what is absent. ``home`` defaults to ``Path.home()``; ``runner``
    and ``which`` default to the stdlib implementations and are injectable so
    no caller is forced to touch the real host.
    """
    repo = Path(repo_root)
    house = Path.home() if home is None else Path(home)
    run = _default_runner if runner is None else runner
    find = shutil.which if which is None else which

    checks, checks_error = _run_checks(repo, run)

    venv_python = _venv_python(repo)
    venv_imports_ok = None
    if venv_python is not None:
        probe = _probe(run, [venv_python, "-c", _IMPORT_PROBE], IMPORTS_TIMEOUT)
        if probe is not None:
            venv_imports_ok = probe[0] == 0

    return {
        "checks": checks,
        "checks_error": checks_error,
        "venv_python": venv_python,
        "venv_imports_ok": venv_imports_ok,
        "mcp_registered": _mcp_registered(run, find),
        "personas": _personas(repo, house),
        "overlord_policy": (house / ".claude" / "overlord-policy.md").is_file(),
        "registry": detect_registry(repo, environ=environ),
        "services": detect_services(repo, runner=run),
    }