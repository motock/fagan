"""Translate one Claude CLI ``stream-json`` stdout line into agent.log lines.

The Claude CLI run with ``--output-format stream-json`` prints one JSON object
per stdout line, but every consumer of ``agent.log`` only understands the
canonical line grammar defined in ``pipeline/agent_log_format.py``:

- ``pipeline/rebrief.py`` ``_log_facts`` scans ``^\\[step (\\d+)\\] ([a-z_]+):``
  and ``^\\[step \\d+\\] bash: (.*)$`` (both ``re.MULTILINE``);
- ``pipeline/build_detect.py`` ``_last_done_summary`` scans the literal
  ``"] DONE:"``;
- ``pipeline/rebrief.py`` ``_current_attempt_log`` scans the literal
  ``"[boot]"``.

``translate_line`` is the pure bridge between the two grammars: one raw
child-stdout line in, zero or more canonical lines out. The step counter is
carried in a caller-owned ``state`` dict (``new_state()`` mints one) so the
translator itself stays stateless and safe to call from a log-drain thread.

This module is PURE and ADDITIVE: nothing here is wired into a caller yet
(that is LOG-05); it defines exactly ``new_state`` and ``translate_line``.
"""

from __future__ import annotations

import json
import re

from pipeline.agent_log_format import format_boot, format_done, format_step

__all__ = ["new_state", "translate_line"]

# Claude emits CamelCase tool names ("Bash", "Read", "Edit", "Web-Search",
# "mcp__fs__read"); the consumer regex wants ``[a-z_]+``. Lowercase, then map
# every non-alphanumeric to "_" so the emitted name always matches.
_TOOL_NAME_CLEAN = re.compile(r"[^a-z0-9]")


def new_state() -> dict:
    """Return a fresh step counter for a new translate session.

    The dict is caller-owned: ``translate_line`` mutates ``state["step"]`` in
    place and never reads or writes any other key.
    """
    return {"step": 0}


def _boot_line(obj: dict) -> list[str]:
    """Translate a ``system``/``init`` event into its ``[boot]`` line, or [].

    ``format_boot`` requires pid and model, so each is passed only when the
    event actually carries a usable value; an init with nothing useful emits
    nothing rather than a boot line full of placeholders.
    """
    pid = obj.get("pid")
    model = obj.get("model")
    session = obj.get("session_id")
    if pid is None and not model and not session:
        return []
    # format_boot renders pid and model unconditionally, so a missing value
    # is passed as its neutral placeholder rather than omitted.
    return [
        format_boot(
            pid=pid if pid is not None else 0,
            model=model if model else "",
            endpoint=session if session else "",
        )
    ]


def _step_lines(obj: dict, state: dict) -> list[str]:
    """Translate an ``assistant`` event into its ``[step N] <tool>: <arg>`` lines.

    Only ``tool_use`` blocks are steps; text-only narration emits nothing and
    leaves the counter untouched. The counter is bumped AFTER each emitted
    line, once per tool_use block, so the value left in ``state`` is correct
    for the next call.
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        tool = _TOOL_NAME_CLEAN.sub("_", name.lower())
        if not tool.strip("_"):
            continue
        tool = tool.strip("_")
        inp = block.get("input")
        if not isinstance(inp, dict):
            inp = {}
        if "command" in inp and isinstance(inp["command"], str):
            arg = inp["command"]
        elif isinstance(inp.get("file_path"), str):
            arg = inp["file_path"]
        elif isinstance(inp.get("path"), str):
            arg = inp["path"]
        else:
            arg = json.dumps(inp, separators=(",", ":"))
        lines.append(format_step(state["step"], tool, arg))
        state["step"] += 1
    return lines


def _done_line(obj: dict, state: dict) -> list[str]:
    """Translate a ``result`` event into its ``[step N] DONE: <summary>`` line.

    The result text is not truncated (``format_done`` applies no 120-char
    rule) and the counter is NOT bumped: only tool_use is a step.
    """
    text = obj.get("result")
    if not isinstance(text, str):
        text = ""
    return [format_done(state["step"], text)]


def translate_line(raw: str, state: dict) -> list[str]:
    """Translate one raw child-stdout line into canonical agent.log lines.

    Returns a list (usually empty or single-element, never ``None``). A line
    that is not valid JSON passes through unchanged as a one-element list so
    the CLI's own plain-text warnings survive verbatim at the top of
    agent.log. Never raises: a missing key, a null, a wrong type, or an
    unexpected shape yields ``[]`` — this runs inside a live log-drain
    thread, where an exception would silently truncate the log.
    """
    state.setdefault("step", 0)
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 - non-JSON passes through verbatim
        return [raw]
    try:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        if kind == "system":
            if obj.get("subtype") == "init":
                return _boot_line(obj)
            return []
        if kind == "assistant":
            return _step_lines(obj, state)
        if kind == "result":
            return _done_line(obj, state)
        return []  # user, tool_result, unknown, missing, non-string type
    except Exception:  # noqa: BLE001 - never raise inside the log-drain thread
        return []