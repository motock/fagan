"""Pure parser for the shared operator env file (.pipeline.env).

The production scheduler is launched by launchd as ``python -m
pipeline.scheduler_daemon`` directly, so nothing sources
``scripts/pipeline-env.sh`` for it. This module lets Python read the same
shell-style env file itself.

Design rules (CFG-E1):
- Pure and side-effect-free: parsing never touches the process environment;
  callers decide what to do with the returned dict.
- Import-safe from ``pipeline/__init__.py``: no imports from the ``pipeline``
  package at all, and no third-party dependencies.
- No expansion: ``~`` and ``$VAR`` are kept literal; callers expand if they
  want to.
"""
from __future__ import annotations

from pathlib import Path

_ENV_FILE_NAME = ".pipeline.env"


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a shell-style env file into a dict.

    Handles blank lines, full-line ``#`` comments, a leading ``export``
    prefix, single- or double-quoted values (exactly ONE matching pair
    stripped), values containing ``=`` (split on the FIRST ``=`` only),
    trailing whitespace, and CRLF line endings. A line with no ``=`` is
    skipped, not an error. A missing or unreadable file (including a
    directory path or undecodable bytes) returns ``{}`` rather than raising.

    Pure: the result is returned to the caller; the process environment is
    never read or written.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Missing file, directory path (IsADirectoryError), permission
        # errors, and undecodable bytes are all "unreadable" -> empty dict.
        return {}

    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip exactly ONE matching quote pair, if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def find_env_file(repo_root: str | Path) -> Path | None:
    """Return ``<repo_root>/.pipeline.env`` if it exists, else ``None``."""
    candidate = Path(repo_root) / _ENV_FILE_NAME
    if candidate.exists():
        return candidate
    return None