"""Regression tests for the LOG-03 review hardening of the Claude translator.

Three review-flagged bugs, all in ``pipeline/claude_log_translate.py`` (the
fix belongs there; this module only reproduces them):

1. BLOCKING: a tool name containing digits (``mcp__db__query_v2``) survives
   the name-clean step (only punctuation is replaced, digits are kept), so
   the emitted ``[step N]`` line's tool token does not satisfy the consumer's
   ``[a-z_]+`` grammar. The step is written to agent.log but every downstream
   scan (``rebrief._log_facts``, the verbatim step regex) silently misses it.
2. An error ``result`` event (``is_error: true`` with no/empty ``result``
   string) yields a useless/empty DONE summary instead of one built from the
   error marker.
3. An embedded ``\\n``/``\\r`` in a tool argument or a result summary splits
   one agent.log entry into several physical lines, corrupting the
   one-entry-per-line grammar and every line-anchored scan.

Until the fix lands, every test here is expected to FAIL for exactly the
reason above — not for an import or syntax error.
"""

from __future__ import annotations

import json
import re

import pytest

from pipeline.build_detect import _last_done_summary
from pipeline.claude_log_translate import new_state, translate_line
from pipeline.rebrief import _log_facts

# The exact consumer regex, copied verbatim from pipeline/rebrief.py
# ``_log_facts`` (``build_detect`` shares the step/tool grammar).
STEP_LINE_RE = re.compile(r"^\[step (\d+)\] ([a-z_]+):", re.MULTILINE)
DONE_LINE_RE = re.compile(r"^\[step (\d+)\] DONE: (.+)$")
DONE_MARKER = "] DONE:"  # pipeline/build_detect.py _last_done_summary

DIGIT_TOOL_NAMES = ["mcp__db__query_v2", "Search2", "tool_4x"]


# --------------------------------------------------------------- helpers


def _tool_use(name, input, block_id="t1"):
    return {"type": "tool_use", "id": block_id, "name": name, "input": input}


def _assistant_event(*blocks):
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def _result_event(**fields):
    payload = {"type": "result", "subtype": "success", "is_error": False}
    payload.update(fields)
    return json.dumps(payload)


def _translate(raw, state):
    """translate_line plus the invariants that hold for EVERY input."""
    out = translate_line(raw, state)
    assert out is not None, "translate_line must return a list, never None"
    assert isinstance(out, list), type(out)
    assert all(isinstance(line, str) for line in out), out
    return out


def _write_log(tmp_path, lines):
    """Write the translated lines as agent.log under <tmp_path>/wt."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return worktree, log_path


# ------------------------------- BLOCKING: digit-containing tool names


@pytest.mark.parametrize("raw_name", DIGIT_TOOL_NAMES)
def test_digit_tool_name_emits_a_token_the_consumer_regex_accepts(raw_name):
    state = new_state()
    raw = _assistant_event(_tool_use(raw_name, {"command": "SELECT 1"}))
    lines = _translate(raw, state)
    assert len(lines) == 1, lines
    match = STEP_LINE_RE.match(lines[0])
    assert match, f"step line invisible to the consumer regex: {lines[0]!r}"
    token = match.group(2)
    assert re.fullmatch(r"[a-z_]+", token), (
        f"tool token {token!r} must satisfy the consumer's [a-z_]+ grammar "
        f"(raw name {raw_name!r}); line: {lines[0]!r}"
    )
    assert state["step"] == 1  # bump-on-tool_use semantics are unchanged


@pytest.mark.parametrize("raw_name", DIGIT_TOOL_NAMES)
def test_digit_tool_name_step_is_recorded_by_rebrief_log_facts(tmp_path, raw_name):
    state = new_state()
    raw = _assistant_event(_tool_use(raw_name, {"command": "SELECT 1"}))
    lines = _translate(raw, state)
    worktree, _ = _write_log(tmp_path, lines)
    facts = _log_facts(worktree)
    assert any("LAST ATTEMPT USED 1 step(s)" in fact for fact in facts), facts
    token = STEP_LINE_RE.match(lines[0]).group(2)
    assert any(f"{token} x1" in fact for fact in facts), facts


# ------------------------------------------- error-result DONE handling


_ERROR_PAYLOADS = [
    {"type": "result", "subtype": "error_during_execution", "is_error": True},
    {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""},
    {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": None},
]


@pytest.mark.parametrize(
    "payload", _ERROR_PAYLOADS, ids=["no-result-key", "empty-result", "null-result"]
)
def test_error_result_emits_exactly_one_usable_done_line(payload):
    state = new_state()
    lines = _translate(json.dumps(payload), state)
    assert len(lines) == 1, lines
    match = DONE_LINE_RE.fullmatch(lines[0])
    assert match, f"not a usable DONE line: {lines[0]!r}"
    summary = match.group(2)
    assert summary.strip(), f"empty DONE summary: {lines[0]!r}"
    assert summary.strip() != "None", f"literal-None DONE summary: {lines[0]!r}"
    assert state["step"] == 0  # DONE must never bump the counter


def test_error_result_done_summary_is_found_by_build_detect(tmp_path):
    state = new_state()
    payload = {"type": "result", "subtype": "error_during_execution", "is_error": True}
    lines = _translate(json.dumps(payload), state)
    _, log_path = _write_log(tmp_path, lines)
    summary = _last_done_summary(log_path)
    assert summary is not None, f"the '] DONE:' scan missed the log: {lines!r}"
    assert summary.strip(), f"empty DONE summary in the translated log: {summary!r}"
    assert summary.strip() != "None"


# ------------------------------------------------- newline hardening


_MULTILINE_ARG_INPUTS = [
    {"command": "echo one\necho two\r\necho three\recho four"},
    {"file_path": "notes\nfrom\r\nclaude\rtool.txt"},
    {"path": "a\rb\nc"},
]


@pytest.mark.parametrize(
    "inp", _MULTILINE_ARG_INPUTS, ids=["command", "file_path", "path"]
)
def test_tool_arg_newlines_never_reach_the_log_line(tmp_path, inp):
    state = new_state()
    tool = "Bash" if "command" in inp else "Write"
    raw = _assistant_event(_tool_use(tool, inp))
    lines = _translate(raw, state)
    assert len(lines) == 1, lines
    for line in lines:
        assert "\n" not in line and "\r" not in line, (
            f"an embedded newline split one agent.log entry: {line!r}"
        )
    assert STEP_LINE_RE.match(lines[0]), lines[0]
    worktree, _ = _write_log(tmp_path, lines)
    text = (worktree / "agent.log").read_text(encoding="utf-8")
    assert len(text.strip().splitlines()) == 1, repr(text)


def test_result_summary_newlines_never_reach_the_log_line(tmp_path):
    state = new_state()
    summary = "fixed the parser\nsecond line\r\nthird line\rfourth line"
    lines = _translate(_result_event(result=summary), state)
    assert len(lines) == 1, lines
    for line in lines:
        assert "\n" not in line and "\r" not in line, (
            f"an embedded newline split one agent.log entry: {line!r}"
        )
    assert DONE_MARKER in lines[0]
    assert state["step"] == 0  # DONE must never bump the counter
    _, log_path = _write_log(tmp_path, lines)
    found = _last_done_summary(log_path)
    assert found is not None, f"the '] DONE:' scan missed the log: {lines!r}"
    assert "second line" in found and "third line" in found, found


# ------------------- counter semantics survive the digit-name fix


def test_worked_trace_digit_fix_leaves_counter_semantics_intact():
    state = new_state()
    assert state["step"] == 0
    out: list[str] = []
    # Call 1: digit-containing tool name -> step 0, counter bumped to 1.
    out += _translate(
        _assistant_event(_tool_use("mcp__db__query_v2", {"command": "SELECT 1"})),
        state,
    )
    assert len(out) == 1, out
    match = STEP_LINE_RE.match(out[0])
    assert match and match.group(1) == "0", out
    assert re.fullmatch(r"[a-z_]+", match.group(2)), out[0]
    # Call 2 (follow-up, same state): DONE reuses the counter, never bumps.
    out += _translate(_result_event(result="ok"), state)
    assert out[1] == "[step 1] DONE: ok", out
    # Call 3 (follow-up, same state): next tool_use emits step 1, counter -> 2.
    out += _translate(
        _assistant_event(
            _tool_use("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
        ),
        state,
    )
    assert len(out) == 3, out
    match = STEP_LINE_RE.match(out[2])
    assert match and match.group(1) == "1" and match.group(2) == "edit", out
    # Call 4 (follow-up, same state): the left-behind counter is 2.
    out += _translate(_result_event(result="done"), state)
    assert out[3] == "[step 2] DONE: done", out
    assert state["step"] == 2