"""The agent.log line grammar — the single definition of the log's line shapes.

Every producer of ``agent.log`` (today ``scripts/local_agent.py``; tomorrow the
Claude translator) must emit lines that satisfy the regexes the existing
consumers already use:

- ``pipeline/rebrief.py`` ``_log_facts``:
  ``r"^\\[step (\\d+)\\] ([a-z_]+):"`` and ``r"^\\[step \\d+\\] bash: (.*)$"``
  (both ``re.MULTILINE``)
- ``pipeline/build_detect.py`` ``_last_done_summary``: the literal ``"] DONE:"``
- ``pipeline/rebrief.py`` ``_current_attempt_log``: the literal ``"[boot]"``

The formatters here return lines WITHOUT a trailing newline; the caller adds
the newline when it writes the line to the log.
"""

from __future__ import annotations

__all__ = ["format_boot", "format_done", "format_step"]


def format_boot(
    *,
    pid: int,
    model: str,
    endpoint: str = "",
    provider: str = "",
    steps: int | None = None,
    timeout: float | None = None,
) -> str:
    """Return the ``[boot] pid=<pid> model=<model> ...`` line.

    Keyword fields are emitted in the fixed order pid, model, endpoint,
    provider, steps, timeout, and only when they were supplied: a value of
    ``""`` or ``None`` is skipped, while falsy numbers such as ``steps=0`` or
    ``timeout=0.0`` are supplied values and must render. A timeout renders
    with a trailing ``s`` (``timeout=8100.0s``).
    """
    parts = ["[boot]", f"pid={pid}", f"model={model}"]
    if endpoint != "":
        parts.append(f"endpoint={endpoint}")
    if provider != "":
        parts.append(f"provider={provider}")
    if steps is not None:
        parts.append(f"steps={steps}")
    if timeout is not None:
        parts.append(f"timeout={timeout}s")
    return " ".join(parts)


def format_step(
    step: int, tool: str, arg: str = "", *, correlation_id: str = ""
) -> str:
    """Return the ``[step <N>] <tool>: <arg>`` line.

    ``arg`` is truncated to 120 characters (the existing producer slices the
    argument with ``[:120]``); the trailing colon-space is kept even when
    ``arg`` is empty, because the consumer regex
    ``^\\[step (\\d+)\\] ([a-z_]+):`` requires the colon. When
    ``correlation_id`` is non-empty, `` [cid=<id>]`` is appended AFTER the
    (already truncated) argument.
    """
    line = f"[step {step}] {tool}: {arg[:120]}"
    if correlation_id:
        line += f" [cid={correlation_id}]"
    return line


def format_done(step: int, summary: str, *, correlation_id: str = "") -> str:
    """Return the ``[step <N>] DONE: <summary>`` line.

    The summary is NOT truncated (the 120-char rule applies only to
    ``format_step``'s argument). The line always contains the literal
    ``"] DONE:"`` marker that ``build_detect._last_done_summary`` scans for.
    """
    line = f"[step {step}] DONE: {summary}"
    if correlation_id:
        line += f" [cid={correlation_id}]"
    return line