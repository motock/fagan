"""Pure module to compute instruction file paths for supported tools.

The module exposes:

* ``SUPPORTED_TOOLS`` – a tuple of the three supported tool names.
* ``instructions_path(tool, env)`` – returns the Path to the instruction file for the
  given tool, using the environment mapping ``env``.
* ``rules_dir(tool, env)`` – returns the sibling directory ``fagan-rules``.
* ``shadowing_path(tool, env)`` – returns the path that would shadow the
  instruction file for Codex (``AGENTS.override.md``) or ``None`` for the other
  tools.

All logic is pure – no I/O, no ```` or ``Path.home``.  The
functions validate input and raise ``ValueError`` for unsupported tools,
missing or empty ``HOME``, relative overrides, etc.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# Public constants
SUPPORTED_TOOLS = ("claude", "codex", "opencode")

# ---------------------------------------------------------------------------
# Internal helpers – not part of the public API
# ---------------------------------------------------------------------------

def _require_tool(tool: str) -> None:
    """Validate that *tool* is one of ``SUPPORTED_TOOLS``.

    Raises ``ValueError`` with a message that contains both the offending
    tool and the ``SUPPORTED_TOOLS`` tuple so that tests can match either.
    """
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(
            f"Unsupported tool: {tool!r}. SUPPORTED_TOOLS={SUPPORTED_TOOLS!r}"
        )


def _require_home(env: Mapping[str, str]) -> str:
    """Return the ``HOME`` value from *env*.

    Raises ``ValueError`` if ``HOME`` is missing or empty.
    """
    home = env.get("HOME")
    if not home:
        raise ValueError("Missing or empty HOME in env")
    return home


def _override(env: Mapping[str, str], key: str) -> str | None:
    """Return an absolute override value or ``None``.

    ``env`` may contain the override key.  If the key is present but the
    value is empty, it is treated as unset.  If the value is present and
    non‑empty, it must be an absolute path – otherwise a ``ValueError`` is
    raised.
    """
    value = env.get(key)
    if not value:
        return None
    if not Path(value).is_absolute():
        raise ValueError(f"Override {key!r} must be absolute: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Configuration directory helpers
# ---------------------------------------------------------------------------

# Claude

def _claude_config_dir(env: Mapping[str, str]) -> Path:
    """Return the configuration directory for Claude.

    If ``CLAUDE_CONFIG_DIR`` is set, it is used; otherwise ``$HOME/.claude``.
    """
    override = _override(env, "CLAUDE_CONFIG_DIR")
    if override is not None:
        return Path(override)
    return Path(_require_home(env)) / ".claude"

# Codex

def _codex_home(env: Mapping[str, str]) -> Path:
    """Return the configuration directory for Codex.

    If ``CODEX_HOME`` is set, it is used; otherwise ``$HOME/.codex``.
    """
    override = _override(env, "CODEX_HOME")
    if override is not None:
        return Path(override)
    return Path(_require_home(env)) / ".codex"

# Opencode

def _opencode_config_dir(env: Mapping[str, str]) -> Path:
    """Return the configuration directory for Opencode.

    Precedence: ``OPENCODE_CONFIG_DIR`` > ``XDG_CONFIG_HOME`` + ``/opencode`` >
    ``$HOME/.config/opencode``.
    """
    override = _override(env, "OPENCODE_CONFIG_DIR")
    if override is not None:
        return Path(override)
    xdg = _override(env, "XDG_CONFIG_HOME")
    if xdg is not None:
        return Path(xdg) / "opencode"
    return Path(_require_home(env)) / ".config" / "opencode"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def instructions_path(tool: str, env: Mapping[str, str]) -> Path:
    """Return the Path to the instruction file for *tool*.

    The function validates *tool* and the environment, then constructs the
    appropriate Path based on the rules described in the tests.
    """
    _require_tool(tool)
    if tool == "claude":
        return _claude_config_dir(env) / "CLAUDE.md"
    if tool == "codex":
        return _codex_home(env) / "AGENTS.md"
    # opencode
    return _opencode_config_dir(env) / "AGENTS.md"


def rules_dir(tool: str, env: Mapping[str, str]) -> Path:
    """Return the sibling ``fagan-rules`` directory for *tool*.

    It is defined as ``instructions_path(tool, env).parent / 'fagan-rules'``.
    """
    return instructions_path(tool, env).parent / "fagan-rules"


def shadowing_path(tool: str, env: Mapping[str, str]) -> Path | None:
    """Return the path that shadows the instruction file for Codex.

    For Codex it is ``<codex_home>/AGENTS.override.md``.  For other tools it
    returns ``None``.  The function is pure and does not check for file
    existence.
    """
    _require_tool(tool)
    if tool != "codex":
        return None
    return _codex_home(env) / "AGENTS.override.md"

# End of module
