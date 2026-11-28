"""Tool-dispatch impls for the oracle agent, split out of local_agent_oracle.py.

run_tool_impl reuses the per-tool handlers of scripts/local_agent_tools.py; the
oracle variant differs only by (a) refusing create_file/str_replace on the
read-only acceptance suite, (b) restoring tampered oracle files after bash,
(c) having no checkpoint tool, and (d) a plain safe_run_tool_impl error message.
Each impl takes an ``origin`` dict (the CALLER's module globals, built fresh in
lao's delegating wrapper on every call), exactly as in local_agent_tools.

This module must NOT import scripts.local_agent_oracle (that would pin the
twin in sys.modules and defeat lao's env-freshness eviction).
"""
from __future__ import annotations

from scripts.local_agent_tools import (
    _TOOL_HANDLERS as _SHARED_HANDLERS,
)
from scripts.local_agent_tools import (  # noqa: F401  (re-exported public constant)
    RESTORE_FILE_MAX_PER_PATH,
    _bash,
)

_ORACLE_GUARDED_TOOLS = ("create_file", "str_replace")


def _guard_oracle_path(handler):
    """Wrap a shared handler so it refuses the read-only acceptance suite."""
    def guarded(origin, args) -> str:
        if origin["is_oracle_path"](args.get("path", "")):
            return (f"ERROR: {args['path']} is the read-only acceptance suite and "
                    f"must NOT be modified. Change the implementation file instead.")
        return handler(origin, args)
    return guarded


def _oracle_bash(origin, args) -> str:
    def restore_tampered() -> str:
        return origin["_restore_tampered_oracle_files"]() if origin["ACCEPTANCE_PATHS"] else ""
    return _bash(origin, args, after_run=restore_tampered)


_TOOL_HANDLERS = {
    name: (_guard_oracle_path(handler) if name in _ORACLE_GUARDED_TOOLS else handler)
    for name, handler in _SHARED_HANDLERS.items()
    if name != "checkpoint"
}
_TOOL_HANDLERS["bash"] = _oracle_bash


def run_tool_impl(origin, fn, args) -> str:
    handler = _TOOL_HANDLERS.get(fn)
    if handler is None:
        return f"unknown tool {fn}"
    return handler(origin, args)


def safe_run_tool_impl(origin, fn, args) -> str:
    """Run a tool, turning any exception into a recoverable error message.

    A model that omits a required argument (e.g. str_replace without old_str,
    observed with weaker local models) would otherwise raise an uncaught
    KeyError and crash the whole unattended agent. Feeding the error back as a
    tool result lets the model correct itself, bounded by the loop guard / step
    cap, instead of taking the run down.
    """
    try:
        # Routed via origin (NOT a direct run_tool_impl call) so a test that
        # monkeypatches scripts.local_agent_oracle.run_tool is honored here.
        return origin["run_tool"](fn, args)
    except Exception as e:  # noqa: BLE001 - a tool call's own failure is reported back to the model as tool output, not raised - the agent loop must never crash on an unpredictable tool error
        return f"ERROR running {fn}: {type(e).__name__}: {e}"