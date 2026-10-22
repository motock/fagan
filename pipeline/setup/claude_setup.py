"""Claude Code integration assets for the guided installer (SETUP-08B).

Three public functions, all free of import-time side effects and all
non-destructive:

``register_mcp_server``
    Register the ``pipeline`` MCP server with Claude Code at user scope, unless
    it is already registered. The server is never removed or re-added.

``install_claude_assets``
    Copy the persona subagents and the overlord policy into ``~/.claude``,
    skipping every destination that already exists.

``install_global_rules``
    Run the opt-in global-rules installer. Only called when the operator
    explicitly opted in; never reached from the other two functions.

This module re-implements the end state of ``scripts/remote-install.sh`` so the
Python wizard can own it; the shell script itself is untouched. Every process
invocation goes through an injectable ``runner(argv, timeout=...) ->
(returncode, stdout, stderr)``, and ``which`` is injectable too, so the whole
surface is testable without the real Claude Code CLI. A subprocess failure
(``OSError``, ``TimeoutExpired``, nonzero exit) is reported in the returned
dict and never raised.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

MCP_SERVER_NAME = "pipeline"
MCP_TIMEOUT_SECONDS = 30
GLOBAL_RULES_TIMEOUT_SECONDS = 120
GLOBAL_RULES_TOOLS: tuple[str, ...] = ("claude", "codex", "opencode")

_DIR_MODE = 0o755
_ERROR_TAIL_CHARS = 500

Runner = Callable[..., tuple[int, str, str]]


def _default_runner(argv: Sequence[str], timeout: float | None = None) -> tuple[int, str, str]:
    """Run *argv* and return ``(returncode, stdout, stderr)``.

    ``check=False``: a nonzero exit is data for the caller, not an exception.
    ``OSError``/``TimeoutExpired`` propagate to :func:`_invoke`, which turns
    them into a ``failed`` result.
    """
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _tail(text: str | None) -> str:
    """Return the last :data:`_ERROR_TAIL_CHARS` characters of *text*, stripped."""
    if not text:
        return ""
    return text.strip()[-_ERROR_TAIL_CHARS:]


def _invoke(
    runner: Runner, argv: Sequence[str], timeout: float
) -> tuple[tuple[int, str, str] | None, str | None]:
    """Call *runner*, converting any subprocess failure into an error string.

    Returns ``((returncode, stdout, stderr), None)`` on success and
    ``(None, message)`` when the process could not be run or timed out.
    """
    try:
        returncode, stdout, stderr = runner(list(argv), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, _tail(str(exc)) or exc.__class__.__name__
    return (returncode, stdout, stderr), None


def _repo_root() -> Path:
    """Repository root: this file lives at ``<repo>/pipeline/setup/``."""
    return Path(__file__).resolve().parents[2]


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) with mode 0755 only when it is missing.

    An existing directory keeps its current mode: setup never rewrites what the
    operator already has.
    """
    if path.is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_DIR_MODE)


def _exists(path: Path) -> bool:
    """True for a regular file *or* a dangling symlink, so neither is clobbered."""
    return path.exists() or path.is_symlink()


def register_mcp_server(
    *,
    repo_root: str | Path,
    home: str | Path | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict:
    """Register the ``pipeline`` MCP server with Claude Code, idempotently.

    Returns one of:

    ``{"status": "skipped", "reason": ...}``
        The venv python or the server module is missing (no ``claude`` call is
        made), or the ``claude`` CLI is not on ``PATH`` (plus ``manual``).
    ``{"status": "kept"}``
        ``claude mcp get pipeline`` succeeded; nothing was added.
    ``{"status": "registered"}``
        ``claude mcp add -s user pipeline <py> <server>`` succeeded.
    ``{"status": "failed", "manual": ..., "error": ...}``
        The add failed or could not be run. Never raises.

    ``claude mcp remove`` is never invoked: an existing registration is always
    left alone.
    """
    root = Path(repo_root)
    # ``home`` is accepted for symmetry with install_claude_assets and so every
    # path this function reports stays inside repo_root/home. The MCP
    # registration itself is user-scoped, so nothing is written under it here.
    run = runner or _default_runner
    find = which or shutil.which

    python = root / ".venv" / "bin" / "python3"
    server = root / "app" / "pipeline_mcp_server.py"
    if not python.is_file():
        return {"status": "skipped", "reason": f"missing venv python at {python}"}
    if not server.is_file():
        return {"status": "skipped", "reason": f"missing MCP server at {server}"}

    manual = f"claude mcp add -s user {MCP_SERVER_NAME} {python} {server}"
    if find("claude") is None:
        return {
            "status": "skipped",
            "reason": "claude CLI not found on PATH",
            "manual": manual,
        }

    get_result, error = _invoke(
        run, ["claude", "mcp", "get", MCP_SERVER_NAME], MCP_TIMEOUT_SECONDS
    )
    if error is not None or get_result is None:
        return {"status": "failed", "manual": manual, "error": error or "claude mcp get failed"}
    if get_result[0] == 0:
        return {"status": "kept"}

    add_argv = [
        "claude",
        "mcp",
        "add",
        "-s",
        "user",
        MCP_SERVER_NAME,
        str(python),
        str(server),
    ]
    add_result, error = _invoke(run, add_argv, MCP_TIMEOUT_SECONDS)
    if error is not None or add_result is None:
        return {"status": "failed", "manual": manual, "error": error or "claude mcp add failed"}
    if add_result[0] == 0:
        return {"status": "registered"}
    return {
        "status": "failed",
        "manual": manual,
        "error": _tail(add_result[2]) or f"claude mcp add exited {add_result[0]}",
    }


def install_claude_assets(
    *, repo_root: str | Path, home: str | Path | None = None
) -> dict:
    """Copy personas and the overlord policy into ``<home>/.claude``.

    Only missing destinations are written; an existing persona or policy is
    kept verbatim and nothing is ever deleted. When the repo has neither an
    ``agents/*.md`` file nor ``overlord-policy.md``, no directory is created.

    Returns ``{"installed": sorted[str], "kept": sorted[str]}`` with the
    destination file names.
    """
    root = Path(repo_root)
    home_dir = Path(home) if home is not None else Path.home()

    agents_src = root / "agents"
    personas = (
        sorted(path for path in agents_src.glob("*.md") if path.is_file())
        if agents_src.is_dir()
        else []
    )
    policy_src = root / "overlord-policy.md"
    has_policy = policy_src.is_file()

    installed: list[str] = []
    kept: list[str] = []
    if not personas and not has_policy:
        return {"installed": [], "kept": []}

    claude_dir = home_dir / ".claude"
    _ensure_dir(claude_dir)

    if personas:
        agents_dst = claude_dir / "agents"
        _ensure_dir(agents_dst)
        for src in personas:
            dst = agents_dst / src.name
            if _exists(dst):
                kept.append(src.name)
            else:
                shutil.copyfile(src, dst)
                installed.append(src.name)

    if has_policy:
        dst = claude_dir / "overlord-policy.md"
        if _exists(dst):
            kept.append(dst.name)
        else:
            shutil.copyfile(policy_src, dst)
            installed.append(dst.name)

    return {"installed": sorted(installed), "kept": sorted(kept)}


def install_global_rules(
    *,
    tools: Iterable[str],
    runner: Runner | None = None,
    dry_run: bool = False,
) -> dict:
    """Run ``scripts/install_global_rules.py`` for the opted-in *tools*.

    The caller must have explicitly opted in; this function is never reached
    from :func:`register_mcp_server` or :func:`install_claude_assets`.

    Raises ``ValueError`` before starting any process when *tools* is empty or
    names an unknown tool. Otherwise returns
    ``{"status": "installed"|"dry-run"|"failed", "error": str|None}``; a
    nonzero exit is reported, never raised.
    """
    requested = tuple(tools)
    if not requested:
        raise ValueError("tools must name at least one tool")
    unknown = [tool for tool in requested if tool not in GLOBAL_RULES_TOOLS]
    if unknown:
        raise ValueError(f"unknown tool(s): {', '.join(unknown)}")

    argv = [
        sys.executable,
        str(_repo_root() / "scripts" / "install_global_rules.py"),
        "--tools=" + ",".join(requested),
    ]
    if dry_run:
        argv.append("--dry-run")

    result, error = _invoke(runner or _default_runner, argv, GLOBAL_RULES_TIMEOUT_SECONDS)
    if error is not None or result is None:
        return {"status": "failed", "error": error or "install_global_rules failed"}
    if result[0] != 0:
        return {
            "status": "failed",
            "error": _tail(result[2]) or f"install_global_rules exited {result[0]}",
        }
    return {"status": "dry-run" if dry_run else "installed", "error": None}
