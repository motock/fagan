"""TDD spec: ``emit_step_line`` must build its line via
``pipeline.agent_log_format.format_step`` — with ZERO change to the bytes.

GOAL (story brief): ``scripts/local_agent.py``'s ``emit_step_line`` currently
builds the ``[step N] ...`` line with its own f-string.  It must instead build
the line via ``pipeline.agent_log_format.format_step`` (the single definition
of the agent.log line grammar), printing and returning byte-identical output.

REQUIRED IMPLEMENTATION CONTRACT (scripts/local_agent.py ONLY — exactly one
production file changes):

1. Add a module-level import near the existing imports::

       from pipeline.agent_log_format import format_step

2. Change ONLY the body of ``emit_step_line``.  Its signature is UNCHANGED —
   ``(step: int, message: str, correlation_id: str = "") -> str`` — because
   many call sites depend on it.  ``emit_step_line`` takes a pre-joined
   ``message`` (e.g. ``"bash: ls -la"``) while ``format_step`` takes ``tool``
   and ``arg`` separately, so the body must split ``message`` on the FIRST
   ``": "`` into ``tool`` and ``arg`` and build the line with
   ``format_step(tool, arg, correlation_id=...)`` (the correlation id is
   forwarded so ``format_step`` appends the ``[cid=...]`` suffix).  When
   ``message`` has NO ``": "`` separator, the body falls back to the current
   f-string (``f"[step {step}] {message}"`` + the cid suffix) so no caller's
   output can change.

3. Every other ``print(...)`` in the file (timeout, LLM-call-failed, nudge,
   parking, DONE inline prints) stays EXACTLY as it is, and no other
   ``scripts/local_agent_*.py`` file (oracle included) is touched.
"""

import ast
import importlib.util
import inspect
import itertools
import re
import sys
from pathlib import Path

import pytest

from pipeline import agent_log_format

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
_LOCAL_AGENT_PY = _SCRIPTS / "local_agent.py"

# The live consumer regex (pipeline/rebrief.py _log_facts, re.MULTILINE).
_CONSUMER_REGEX = re.compile(r"^\[step (\d+)\] ([a-z_]+):")

# Lower bound for pre-existing inline ``print(f"[step ...")`` calls OUTSIDE
# emit_step_line (timeout / LLM-failed / nudge / parking / rework-cap / ...).
# Deliberately a floor, not an exact count: this story must not remove any of
# them, but later stories may add more.
_MIN_OTHER_STEP_PRINTS = 10

_counter = itertools.count()


def _exec_module(path: Path, modname: str):
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


def _load_agent(monkeypatch):
    """Load scripts/local_agent.py fresh under a private name.

    Mirrors tests/unit/test_w4l_agent_log_correlation.py: the module reads env
    at import time, so pin the environment before it executes.
    """
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    monkeypatch.delenv("PIPELINE_CORRELATION_ID", raising=False)
    return _exec_module(
        _LOCAL_AGENT_PY, f"_local_agent_emit_step_test_{next(_counter)}"
    )


def _source() -> str:
    return _LOCAL_AGENT_PY.read_text()


def _emit_step_line_node(tree: ast.AST) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "emit_step_line":
            return node
    pytest.fail("scripts/local_agent.py must define emit_step_line")


def _spy_format_step(monkeypatch, la):
    """Replace format_step with a recording wrapper that delegates to the real
    function.  Both bindings are patched (the ``la`` from-import binding AND
    the ``pipeline.agent_log_format`` module attribute) so the spy intercepts
    the call whichever way the body references it.

    Returns ``(calls, real)`` where calls is a list of ``(args, kwargs)``
    tuples from each intercepted call.
    """
    real = la.format_step  # AttributeError here == implementation missing (RED)
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(la, "format_step", spy)
    monkeypatch.setattr(agent_log_format, "format_step", spy)
    return calls, real


def _bound_step_tool_arg_cid(real, args, kwargs):
    """Bind one intercepted call against format_step's real signature and
    return (step, tool, arg, correlation_id), whatever positional/keyword mix
    the caller used."""
    bound = inspect.signature(real).bind(*args, **kwargs)
    bound.apply_defaults()
    a = bound.arguments
    return a["step"], a["tool"], a["arg"], a["correlation_id"]


# ---------------------------------------------------------------------------
# Positive: byte-identical output for the documented call shapes
# ---------------------------------------------------------------------------


def test_bash_message_returns_exact_bytes(monkeypatch, capsys):
    la = _load_agent(monkeypatch)
    line = la.emit_step_line(3, "bash: ls -la")
    assert line == "[step 3] bash: ls -la"
    assert capsys.readouterr().out == "[step 3] bash: ls -la\n"


def test_create_file_message_at_step_zero(monkeypatch, capsys):
    la = _load_agent(monkeypatch)
    line = la.emit_step_line(0, "create_file: .agent_scratchpad.md")
    assert line == "[step 0] create_file: .agent_scratchpad.md"
    assert capsys.readouterr().out == "[step 0] create_file: .agent_scratchpad.md\n"


def test_cid_suffix_appended_after_argument(monkeypatch):
    la = _load_agent(monkeypatch)
    line = la.emit_step_line(1, "bash: ls", "abc123")
    assert line.endswith(" [cid=abc123]")
    assert line == "[step 1] bash: ls [cid=abc123]"


def test_returned_string_is_exactly_what_is_printed(monkeypatch, capsys):
    la = _load_agent(monkeypatch)
    for step, message, cid in (
        (3, "bash: ls -la", ""),
        (2, "checkpoint", ""),
        (1, "bash: ls", "abc123"),
        (0, "create_file: .agent_scratchpad.md", ""),
    ):
        returned = la.emit_step_line(step, message, correlation_id=cid)
        captured = capsys.readouterr()
        assert captured.out == returned + "\n", (
            f"stdout must be the returned line plus a newline for "
            f"step={step} message={message!r}"
        )


def test_output_satisfies_live_consumer_regex(monkeypatch):
    la = _load_agent(monkeypatch)
    m = _CONSUMER_REGEX.match(la.emit_step_line(3, "bash: ls -la"))
    assert m is not None, "output must match ^\\[step (\\d+)\\] ([a-z_]+):"
    assert m.group(1) == "3"
    assert m.group(2) == "bash"
    m2 = _CONSUMER_REGEX.match(
        la.emit_step_line(0, "create_file: .agent_scratchpad.md")
    )
    assert m2 is not None
    assert m2.group(1) == "0"
    assert m2.group(2) == "create_file"


# ---------------------------------------------------------------------------
# Delegation: the line is built by format_step, fed the split tool/arg
# ---------------------------------------------------------------------------


def test_emit_step_line_calls_format_step_with_split_tool_and_arg(monkeypatch):
    la = _load_agent(monkeypatch)
    calls, real = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(3, "bash: ls -la")
    assert len(calls) == 1, (
        "emit_step_line must route ': '-separated messages through "
        "pipeline.agent_log_format.format_step"
    )
    step, tool, arg, cid = _bound_step_tool_arg_cid(real, *calls[0])
    assert (step, tool, arg, cid) == (3, "bash", "ls -la", "")
    assert line == "[step 3] bash: ls -la"


def test_correlation_id_is_forwarded_to_format_step(monkeypatch):
    la = _load_agent(monkeypatch)
    calls, real = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(1, "bash: ls", "abc123")
    assert len(calls) == 1
    step, tool, arg, cid = _bound_step_tool_arg_cid(real, *calls[0])
    assert (step, tool, arg, cid) == (1, "bash", "ls", "abc123")
    assert line == "[step 1] bash: ls [cid=abc123]"


def test_split_on_first_colon_space_only(monkeypatch):
    la = _load_agent(monkeypatch)
    calls, real = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(1, "bash: echo a: b")
    assert len(calls) == 1
    _, tool, arg, _ = _bound_step_tool_arg_cid(real, *calls[0])
    assert tool == "bash"
    assert arg == "echo a: b"
    assert line == "[step 1] bash: echo a: b"


def test_no_separator_message_skips_format_step(monkeypatch):
    la = _load_agent(monkeypatch)
    calls, _ = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(2, "checkpoint")
    assert calls == [], "the no-': ' fallback must not call format_step"
    assert line == "[step 2] checkpoint"


def test_empty_message_skips_format_step(monkeypatch):
    la = _load_agent(monkeypatch)
    calls, _ = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(5, "")
    assert calls == []
    assert line == "[step 5] "  # trailing space is significant


def test_colon_without_space_is_not_a_separator(monkeypatch):
    # The split separator is literally ": ": "bash:" has no separator and must
    # take the f-string fallback (format_step would add a trailing space).
    la = _load_agent(monkeypatch)
    calls, _ = _spy_format_step(monkeypatch, la)
    line = la.emit_step_line(4, "bash:")
    assert calls == []
    assert line == "[step 4] bash:"


# ---------------------------------------------------------------------------
# Negative / boundary: pre-existing behaviour, unchanged
# ---------------------------------------------------------------------------


def test_no_separator_message_keeps_preexisting_output(monkeypatch):
    la = _load_agent(monkeypatch)
    assert la.emit_step_line(2, "checkpoint") == "[step 2] checkpoint"


def test_empty_message_keeps_preexisting_output(monkeypatch):
    la = _load_agent(monkeypatch)
    assert la.emit_step_line(5, "") == "[step 5] "


def test_default_correlation_id_adds_no_cid_suffix(monkeypatch, capsys):
    la = _load_agent(monkeypatch)
    line = la.emit_step_line(3, "bash: pwd")
    assert line == "[step 3] bash: pwd"
    assert "[cid=" not in line
    assert "[cid=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Source-level: the mechanically-checkable shape of the change
# ---------------------------------------------------------------------------


def test_module_imports_format_step_from_pipeline_agent_log_format():
    src = _source()
    import_line = "from pipeline.agent_log_format import format_step"
    assert import_line in src, (
        "scripts/local_agent.py must import format_step from "
        "pipeline.agent_log_format"
    )
    assert src.index(import_line) < src.index("def emit_step_line"), (
        "the format_step import must be module-level (near the existing "
        "imports), not a function-local import inside emit_step_line"
    )


def test_la_format_step_is_the_pipeline_function(monkeypatch):
    la = _load_agent(monkeypatch)
    assert la.format_step is agent_log_format.format_step, (
        "emit_step_line must delegate to pipeline.agent_log_format.format_step "
        "itself, not a local reimplementation"
    )


def test_emit_step_line_defined_exactly_once_with_unchanged_signature(
    monkeypatch,
):
    src = _source()
    assert src.count("def emit_step_line") == 1
    la = _load_agent(monkeypatch)
    params = inspect.signature(la.emit_step_line).parameters
    assert list(params) == ["step", "message", "correlation_id"]
    assert params["step"].default is inspect.Parameter.empty
    assert params["message"].default is inspect.Parameter.empty
    assert params["correlation_id"].default == ""
    assert params["correlation_id"].kind is not inspect.Parameter.POSITIONAL_ONLY


def test_emit_step_line_body_keeps_single_print_with_flush():
    src = _source()
    node = _emit_step_line_node(ast.parse(src))
    prints = [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "print"
    ]
    assert len(prints) == 1, "emit_step_line must keep exactly one print call"
    segment = ast.get_source_segment(src, prints[0]) or ""
    assert "flush=True" in segment


def test_other_inline_step_prints_are_untouched():
    src = _source()
    tree = ast.parse(src)
    node = _emit_step_line_node(tree)
    start, end = node.lineno, node.end_lineno
    outside = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "print"
        and not (start <= n.lineno <= end)
        and "[step" in (ast.get_source_segment(src, n) or "")
    ]
    assert len(outside) >= _MIN_OTHER_STEP_PRINTS, (
        "the other inline print(f\"[step ...]\") calls (timeout, LLM-failed, "
        "nudge, parking, DONE, ...) must be left exactly as they are"
    )
    # One concrete anchor from the live file: the parking print survives.
    assert 'rework suite-reject cap "' in src


def test_emit_step_line_call_site_survives():
    src = _source()
    assert src.count("emit_step_line(") >= 2  # the def + the DONE call site


def test_no_other_local_agent_sibling_file_touched():
    for path in sorted(_SCRIPTS.glob("local_agent_*.py")):
        if path.name == "local_agent.py":
            continue
        text = path.read_text()
        assert "agent_log_format" not in text, (
            f"{path.name} must not be touched by this story"
        )
        assert "format_step" not in text, (
            f"{path.name} must not be touched by this story"
        )


# ---------------------------------------------------------------------------
# Regression: ": " arguments longer than 120 chars must stay verbatim
# ---------------------------------------------------------------------------


def test_done_summary_over_120_chars_is_verbatim(monkeypatch, capsys):
    """Regression (review blocking): ``emit_step_line`` routes ``": "``-
    separated messages through ``pipeline.agent_log_format.format_step``,
    which truncates its argument to ``arg[:120]``.  The OLD ``emit_step_line``
    never truncated, so byte-identity breaks exactly for arguments longer
    than 120 chars.  The only production call site passes
    ``f"DONE: {args.get('summary', '')}"`` — free-form text — so a surrender
    phrase beyond char 120 must still reach agent.log verbatim:
    ``build_detect._last_done_summary`` prints the DONE summary argument
    verbatim and ``story_status`` feeds it to ``_is_give_up_summary`` for
    ``failure_kind="give_up"`` classification.
    """
    la = _load_agent(monkeypatch)
    arg = "R" * 120 + " I give up"  # 130 chars: 10 past the 120-char cut
    message = "DONE: " + arg  # 136 chars
    assert len(arg) == 130
    assert len(message) == 136
    line = la.emit_step_line(7, message)
    expected = f"[step 7] {message}"
    assert line == expected, (
        "emit_step_line must render ': '-separated messages verbatim with no "
        f"truncation; got {line!r}, want {expected!r} (the ' I give up' tail "
        "beyond char 120 must not be dropped)"
    )
    assert " I give up" in line
    # Follow-up call: the opt-out must be per-call (keyword argument), so a
    # short DONE immediately after still renders byte-identically and the log
    # contains both lines in full, in order — no truncation state carries
    # between calls.
    line2 = la.emit_step_line(8, "DONE: ok")
    assert line2 == "[step 8] DONE: ok"
    assert capsys.readouterr().out == line + "\n" + line2 + "\n"


def test_done_summary_134_chars_attempt_failed_gives_up_is_verbatim(
    monkeypatch, capsys
):
    """Regression (review blocking): the argument after ``"DONE: "`` is
    15*8 == 120 padding chars + 8 chars of ``"gives up"`` == 128 chars, so
    the full message is 134 chars.  ``format_step`` slices ``arg[:120]``, so
    the pre-fix line is ``"DONE: " + arg[:120]`` (126 chars of message,
    ending ``"...attempt failed "``) — ``"gives up"`` (message chars 127-134)
    is dropped.  Downstream the damage is permanent: agent.log persists, and
    ``build_detect._last_done_summary`` takes the summary verbatim into
    ``story_status`` -> ``_is_give_up_summary`` (parsers.py:285), so a
    surrender phrase past char 120 is invisible and
    ``failure_kind="give_up"`` is missed.  The fix must hold per-call, so a
    follow-up ``"DONE: ok"`` call must also be written verbatim.
    """
    la = _load_agent(monkeypatch)
    message = "DONE: " + ("attempt failed " * 8) + "gives up"
    assert len(message) == 134
    line = la.emit_step_line(9, message)
    expected = f"[step 9] {message}"
    assert line == expected, (
        f"emit_step_line must render the DONE summary verbatim; got "
        f"{line!r} ({len(line)} chars), want {expected!r} (134-char message "
        "verbatim — 'gives up' must not be cut at char 120)"
    )
    assert line.endswith(message)
    # Follow-up call: the fix must hold per-call, not just once.
    line2 = la.emit_step_line(10, "DONE: ok")
    assert line2 == "[step 10] DONE: ok"
    assert capsys.readouterr().out == line + "\n" + line2 + "\n"
