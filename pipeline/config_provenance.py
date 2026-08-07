"""
Module providing read‑only accessors for configuration values from three sources:

* The launchd scheduler plist's ``EnvironmentVariables`` block.
* The MCP server's ``env`` block in the user’s ~/.claude.json file.
* Code defaults (not implemented here – this module only reads files).

The module is intentionally lightweight and imports only standard‑library modules. It must not import any of the orchestrator or dashboard code to avoid import cycles.
"""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import xml.parsers.expat

Path = pathlib.Path
# ---------------------------------------------------------------------------
# Path helpers – lazily resolve env overrides so that tests can monkeypatch the
# environment or ``Path.home`` without affecting module import time.
# ---------------------------------------------------------------------------

def _scheduler_plist_path() -> Path:
    """Return the path to the launchd scheduler plist.

    If ``PIPELINE_SCHEDULER_PLIST_PATH`` is set, that value is used.  Otherwise
    the default is ``~/Library/LaunchAgents/com.claude.pipeline.advance-scheduler.plist``.
    The resolution happens inside the function so callers can monkeypatch the
    environment after import.
    """
    env_path = os.getenv("PIPELINE_SCHEDULER_PLIST_PATH")
    if env_path:
        return Path(env_path).resolve()
    # Default path – use ``Path.home`` at call time to allow tests to monkeypatch it.
    default = Path.home() / "Library" / "LaunchAgents" / "com.claude.pipeline.advance-scheduler.plist"
    return default.resolve()


def _claude_json_path() -> Path:
    """Return the path to the MCP server JSON configuration file.

    If ``PIPELINE_CLAUDE_JSON_PATH`` is set, that value is used.  Otherwise the
    default is ``~/.claude.json``.
    """
    env_path = os.getenv("PIPELINE_CLAUDE_JSON_PATH")
    if env_path:
        return Path(env_path).resolve()
    default = Path.home() / ".claude.json"
    return default.resolve()

# ---------------------------------------------------------------------------
# Diagnostic readers – never raise, always return ``dict[str,str]``.
# ---------------------------------------------------------------------------

def read_plist_env(path: Path | None = None) -> dict[str, str]:
    """Read the ``EnvironmentVariables`` dictionary from a launchd plist.

    Parameters
    ----------
    path:
        The file to read.  If ``None`` the default scheduler plist is used.

    Returns
    -------
    dict[str, str]
        Mapping of environment variable names to string values.  All values are
        coerced with ``str()``.  On any error (file missing, permission
        denied, malformed XML, etc.) an empty dictionary is returned.
    """
    if path is None:
        path = _scheduler_plist_path()
    try:
        data_bytes = path.read_bytes()
        data = plistlib.loads(data_bytes)
    except (OSError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError, ValueError):
        return {}
    # ``data`` should be a dict; if not, bail.
    if not isinstance(data, dict):
        return {}
    env = data.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return {}
    # Coerce all values to str.
    return {k: str(v) for k, v in env.items()}


def read_mcp_server_env(path: Path | None = None, server_name: str = "pipeline") -> dict[str, str]:
    """Read the ``env`` block from a MCP server JSON configuration.

    Parameters
    ----------
    path:
        The file to read.  If ``None`` the default ~/.claude.json is used.
    server_name:
        Name of the server entry to look up under ``mcpServers``.

    Returns
    -------
    dict[str, str]
        Mapping of environment variable names to string values.  All values are
        coerced with ``str()``.  On any error an empty dictionary is returned.
    """
    if path is None:
        path = _claude_json_path()
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    # Validate structure step by step.
    if not isinstance(payload, dict):
        return {}
    mcp_servers = payload.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return {}
    server_entry = mcp_servers.get(server_name)
    if not isinstance(server_entry, dict):
        return {}
    env_block = server_entry.get("env")
    if not isinstance(env_block, dict):
        return {}
    return {k: str(v) for k, v in env_block.items()}

# End of module.
