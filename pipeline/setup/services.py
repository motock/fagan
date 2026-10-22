"""Render, optionally enable, start, and health-verify the platform services.

Guided setup has to leave a working install behind: the launchd plists (macOS)
or systemd units (Linux) rendered from the repo's templates, the scheduler
registered with the init system, the dashboard and scheduler started, and the
dashboard's health endpoint answering. This module does exactly those four
things and nothing else.

Design rules, all of them load-bearing:

* Every host process goes through an injectable ``runner(argv, timeout=...,
  env=None) -> (returncode, stdout, stderr)``. ``shell=True`` is never used and
  every call carries a timeout, so a wedged generator or a hung ``systemctl``
  cannot hang the wizard.
* Every failure is *data*. A nonzero exit, a timeout, a missing executable, or
  a refused connection is reported in the returned dict/tuple; nothing is
  raised at the caller except the ``ValueError`` for a caller-supplied invalid
  port/attempt count, which is a programming error rather than a host
  condition.
* The pinned generators keep their own contracts. In particular
  ``scripts/generate_launchd_plists.sh`` fails closed without an MLX model
  path; this module detects that case up front and reports a skip instead of
  invoking the generator with a workaround, so the pinned fail-closed behavior
  is preserved rather than papered over.
* The dashboard API key is a secret. It is sent in the request header and
  nowhere else: it is never logged, never returned, and is scrubbed out of any
  error text that might echo it back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

__all__ = ["render_services", "start_services", "verify_health"]

#: The scheduler's service label, identical on launchd and systemd.
LABEL = "com.fagan.pipeline.advance-scheduler"

#: Generators and start scripts are slow but must never be unbounded.
GENERATOR_TIMEOUT = 120
START_TIMEOUT = 120
HEALTH_TIMEOUT = 5.0

#: How much of a process's output is kept for reporting.
TAIL_CHARS = 2000

#: Placeholder substituted for the API key in any reported text.
REDACTED = "***"

#: Injectable process runner: ``(returncode, stdout, stderr)``.
Runner = Callable[..., tuple[int, str, str]]

_DARWIN_SKIP_REASON = (
    "launchd rendering requires MLX_MODEL_PATH (used by the optional "
    "mlx-supervisor job); set it and re-run, or start services in this session"
)

_DARWIN_RENDERED_SUFFIXES = (".plist",)
_DARWIN_EXTRA_RENDERED = ("pipeline-logs.newsyslog.conf",)
_LINUX_RENDERED_SUFFIXES = (".service", ".timer")
_LINUX_EXTRA_RENDERED = ("pipeline-logs.logrotate.conf",)

#: Every rendered service definition is named after the pipeline label.
_RENDERED_PREFIX = "com.fagan.pipeline."


def _tail(text: str | None, limit: int = TAIL_CHARS) -> str:
    """Return the last ``limit`` characters of ``text``, stripped."""
    if not text:
        return ""
    return text[-limit:].strip()


def _default_runner(
    argv: Sequence[str],
    timeout: int | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``argv`` and report ``(returncode, stdout, stderr)``.

    The only place this module starts a host process. ``shell=True`` is never
    used, the call is always bounded by ``timeout``, and a missing executable
    or a timeout collapses to a nonzero status instead of an exception.
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else dict(env),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return (127, "", str(exc))
    return (completed.returncode, completed.stdout or "", completed.stderr or "")


def _call(
    runner: Runner, argv: Sequence[str], timeout: int, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    """Invoke ``runner`` and normalise any escape into a nonzero result."""
    try:
        result = runner(list(argv), timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001 - a broken runner must not abort setup
        return (1, "", f"{type(exc).__name__}: {exc}")
    try:
        returncode, stdout, stderr = result
    except (TypeError, ValueError):
        return (1, "", "runner returned a malformed result")
    return (int(returncode), stdout or "", stderr or "")


def _failure_text(returncode: int, stdout: str, stderr: str) -> str:
    """Build a short, non-empty description of a failed process call."""
    text = _tail(stderr) or _tail(stdout)
    if text:
        return text
    return f"command exited with status {returncode}"


def _collect_rendered(
    out_dir: Path, suffixes: tuple[str, ...], extras: tuple[str, ...]
) -> list[str]:
    """List the rendered file names that actually exist in ``out_dir``."""
    try:
        entries = sorted(out_dir.iterdir())
    except OSError:
        return []
    names = [
        entry.name
        for entry in entries
        if entry.is_file()
        and (
            (entry.name.startswith(_RENDERED_PREFIX) and entry.name.endswith(suffixes))
            or entry.name in extras
        )
    ]
    return sorted(names)


def _render_result(
    *,
    platform: str,
    status: str,
    rendered: list[str] | None = None,
    enabled: bool | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> dict:
    """Assemble the uniform ``render_services`` result dict."""
    return {
        "platform": platform,
        "status": status,
        "rendered": list(rendered or []),
        "enabled": enabled,
        "reason": reason,
        "error": error,
    }


def _enable_darwin(repo_root: Path, runner: Runner) -> tuple[bool, str | None]:
    """Register the scheduler plist with launchd unless it already is."""
    returncode, stdout, stderr = _call(runner, ["launchctl", "list"], GENERATOR_TIMEOUT)
    if returncode == 0 and LABEL in stdout:
        return (True, None)

    plist = repo_root / "launchd" / f"{LABEL}.plist"
    returncode, stdout, stderr = _call(
        runner, ["launchctl", "load", "-w", str(plist)], GENERATOR_TIMEOUT
    )
    if returncode != 0:
        return (False, _failure_text(returncode, stdout, stderr))
    return (True, None)


def _enable_linux(runner: Runner) -> tuple[bool, str | None]:
    """Reload the user systemd manager and enable the scheduler unit."""
    returncode, stdout, stderr = _call(
        runner, ["systemctl", "--user", "daemon-reload"], GENERATOR_TIMEOUT
    )
    if returncode != 0:
        return (False, _failure_text(returncode, stdout, stderr))

    returncode, stdout, stderr = _call(
        runner,
        ["systemctl", "--user", "enable", "--now", f"{LABEL}.service"],
        GENERATOR_TIMEOUT,
    )
    if returncode != 0:
        return (False, _failure_text(returncode, stdout, stderr))
    return (True, None)


def render_services(
    *,
    repo_root: str | os.PathLike[str],
    platform: str | None = None,
    mlx_model_path: str | None = None,
    enable: bool = False,
    runner: Runner | None = None,
) -> dict:
    """Render the platform's service definitions and optionally enable them.

    Returns ``{"platform", "status", "rendered", "enabled", "reason",
    "error"}`` where ``status`` is ``"rendered"``, ``"skipped"``, or
    ``"failed"``. Nothing is enabled unless ``enable`` is true *and* the render
    succeeded, and no failure is ever raised.
    """
    resolved_platform = platform or sys.platform
    resolved_runner = runner or _default_runner
    root = Path(repo_root)

    if resolved_platform == "darwin":
        if not (mlx_model_path or "").strip():
            return _render_result(
                platform=resolved_platform,
                status="skipped",
                reason=_DARWIN_SKIP_REASON,
            )
        out_dir = root / "launchd"
        argv = [
            "bash",
            str(root / "scripts" / "generate_launchd_plists.sh"),
            "--repo-root",
            str(root),
            "--out-dir",
            str(out_dir),
            "--mlx-model-path",
            str(mlx_model_path),
        ]
        suffixes = _DARWIN_RENDERED_SUFFIXES
        extras = _DARWIN_EXTRA_RENDERED
    elif resolved_platform == "linux":
        out_dir = root / "systemd"
        argv = [
            "bash",
            str(root / "scripts" / "generate_systemd_units.sh"),
            "--repo-root",
            str(root),
            "--out-dir",
            str(out_dir),
        ]
        suffixes = _LINUX_RENDERED_SUFFIXES
        extras = _LINUX_EXTRA_RENDERED
    else:
        return _render_result(
            platform=resolved_platform,
            status="skipped",
            reason=f"{resolved_platform} is not supported for service rendering",
        )

    returncode, stdout, stderr = _call(
        resolved_runner, argv, GENERATOR_TIMEOUT
    )
    if returncode != 0:
        return _render_result(
            platform=resolved_platform,
            status="failed",
            error=_failure_text(returncode, stdout, stderr),
        )

    rendered = _collect_rendered(out_dir, suffixes, extras)
    if not enable:
        return _render_result(
            platform=resolved_platform, status="rendered", rendered=rendered
        )

    if resolved_platform == "darwin":
        enabled, error = _enable_darwin(root, resolved_runner)
    else:
        enabled, error = _enable_linux(resolved_runner)
    return _render_result(
        platform=resolved_platform,
        status="rendered",
        rendered=rendered,
        enabled=enabled,
        error=error,
    )


def _validate_port(port: object) -> int:
    """Return ``port`` as an int in 1..65535 or raise ``ValueError``."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(  # noqa: TRY004 - callers expect ValueError for a bad port
            f"port must be an integer, got {type(port).__name__}"
        )
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {port}")
    return port


def _start_one(
    runner: Runner,
    argv: Sequence[str],
    env: Mapping[str, str] | None,
) -> dict:
    """Run one start script and report ``{"ok", "output"}``."""
    returncode, stdout, stderr = _call(runner, argv, START_TIMEOUT, env=env)
    output = _tail("\n".join(part for part in (stdout, stderr) if part))
    return {"ok": returncode == 0, "output": output}


def start_services(
    *,
    repo_root: str | os.PathLike[str],
    port: int,
    runner: Runner | None = None,
) -> dict:
    """Start the dashboard and the scheduler, reporting each independently.

    ``DASHBOARD_PORT`` is passed to the dashboard call only, merged over a copy
    of ``os.environ``; ``os.environ`` itself is never mutated. One script
    failing does not prevent the other from running.
    """
    resolved_port = _validate_port(port)
    resolved_runner = runner or _default_runner
    root = Path(repo_root)

    dashboard_env = dict(os.environ)
    dashboard_env["DASHBOARD_PORT"] = str(resolved_port)

    dashboard = _start_one(
        resolved_runner,
        ["bash", str(root / "scripts" / "dashboard.sh"), "start"],
        dashboard_env,
    )
    scheduler = _start_one(
        resolved_runner,
        ["bash", str(root / "scripts" / "scheduler.sh"), "start"],
        None,
    )
    return {"dashboard": dashboard, "scheduler": scheduler, "port": resolved_port}


def _redact(text: str, api_key: str) -> str:
    """Remove ``api_key`` from ``text`` so it can never be reported."""
    if api_key and api_key in text:
        return text.replace(api_key, REDACTED)
    return text


def _response_status(response: object) -> int | None:
    """Best-effort HTTP status from a urlopen-style response object."""
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        try:
            code = getcode()
        except Exception:  # noqa: BLE001 - a broken response is just unhealthy
            return None
        if isinstance(code, int):
            return code
    return None


def _close_quietly(response: object) -> None:
    """Close a response, ignoring any error from a half-open socket."""
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: S110, BLE001 - closing must never mask the result
            pass


def _probe_once(
    opener: Callable[..., object],
    request: urllib.request.Request,
) -> tuple[bool, str]:
    """Perform one health request; return ``(healthy, reason)``."""
    try:
        response = opener(request, timeout=HEALTH_TIMEOUT)
    except urllib.error.HTTPError as exc:
        return (False, f"dashboard returned HTTP {exc.code}")
    except OSError as exc:
        return (False, f"dashboard unreachable: {exc}")

    try:
        status = _response_status(response)
        if status != 200:
            return (False, f"dashboard returned HTTP {status}")
        try:
            body = response.read()
        except OSError as exc:
            return (False, f"dashboard health read failed: {exc}")
        try:
            json.loads(body)
        except (ValueError, TypeError):
            return (False, "dashboard health response was not valid JSON")
        return (True, "dashboard healthy")
    finally:
        _close_quietly(response)


def verify_health(
    *,
    port: int,
    api_key: str,
    attempts: int = 30,
    delay: float = 1.0,
    opener: Callable[..., object] | None = None,
    sleep: Callable[[float], object] | None = None,
) -> tuple[bool, str]:
    """Poll the dashboard's health endpoint until it answers or attempts run out.

    Returns ``(True, "dashboard healthy")`` on a 200 response carrying valid
    JSON, otherwise ``(False, <short reason>)``. The API key travels only in
    the request header and is scrubbed from every reported string.
    """
    resolved_port = _validate_port(port)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError(f"attempts must be a positive integer, got {attempts!r}")

    resolved_opener = opener or urllib.request.urlopen
    resolved_sleep = sleep or time.sleep
    secret = api_key or ""

    url = f"http://127.0.0.1:{resolved_port}/api/health"
    reason = "dashboard health check did not complete"

    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"x-pipeline-api-key": secret}, method="GET"
        )
        healthy, reason = _probe_once(resolved_opener, request)
        if healthy:
            return (True, reason)
        if attempt + 1 < attempts:
            resolved_sleep(delay)

    return (False, _redact(reason, secret))
