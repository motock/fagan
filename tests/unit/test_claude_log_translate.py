"""Tests for ``pipeline.claude_log_translate`` (stream-json -> agent.log).

The Claude CLI's ``--output-format stream-json`` emits one JSON object per
stdout line, but the pipeline's consumers only understand the canonical
``agent.log`` grammar defined in ``pipeline/agent_log_format.py``:
``pipeline/rebrief.py`` ``_log_facts`` scans ``^\\[step (\\d+)\\] ([a-z_]+):``
and ``^\\[step \\d+\\] bash: (.*)$``, and ``pipeline/build_detect.py``
``_last_done_summary`` scans the literal ``"] DONE:"``.  ``translate_line``
is the pure bridge between the two grammars: one raw child-stdout line in,
zero or more canonical lines out, with the step counter carried in a
caller-owned ``state`` dict.

Contracts graded here:

- non-JSON lines pass through UNCHANGED (the CLI's own plain-text warnings
  must survive verbatim at the top of agent.log);
- ``assistant`` ``tool_use`` blocks become ``[step N] <tool>: <arg>`` lines
  with the tool name lowercased/underscore-cleaned so the consumer regex
  matches (Claude emits ``Bash``/``Read``/``Edit``; the regex wants
  ``[a-z_]+``);
- ``result`` becomes a ``"] DONE:"`` line; ``system``/``init`` becomes a
  ``[boot]`` line when anything useful is present, else nothing;
- everything else (``user``, ``tool_result``, unknown type, no ``type``,
  malformed shapes) emits nothing;
- the function NEVER raises - it runs inside a live log-drain thread where
  an exception would silently truncate the log;
- the story is PURE and ADDITIVE: no existing module may wire the translator.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from pipeline.agent_log_format import format_done, format_step
from pipeline.build_detect import _last_done_summary
from pipeline.claude_log_translate import new_state, translate_line
from pipeline.rebrief import _log_facts

# The exact consumer regexes, copied verbatim from pipeline/rebrief.py.
STEP_LINE_RE = re.compile(r"^\[step (\d+)\] ([a-z_]+):", re.MULTILINE)
BASH_LINE_RE = re.compile(r"^\[step \d+\] bash: (.*)$", re.MULTILINE)
DONE_MARKER = "] DONE:"  # pipeline/build_detect.py _last_done_summary
BOOT_MARKER = "[boot]"  # pipeline/rebrief.py _current_attempt_log

_SUMMARY = "Tightened the parser grammar and left the suite green"


# --------------------------------------------------------------- fixtures


def _text_block(text="Let me look at this first."):
    return {"type": "text", "text": text}


def _tool_use(name, input, block_id="t1"):
    return {"type": "tool_use", "id": block_id, "name": name, "input": input}


def _assistant_event(*blocks):
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def _translate(raw, state):
    """translate_line plus the invariants that hold for EVERY input."""
    out = translate_line(raw, state)
    assert out is not None, "translate_line must return a list, never None"
    assert isinstance(out, list), type(out)
    assert all(isinstance(line, str) for line in out), out
    return out


def _realistic_stream_lines():
    """A realistic child stream: boot, five tool calls, then done."""
    return [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-9f2c",
                "model": "claude-sonnet-4-5",
                "cwd": "/tmp/worktree",
            }
        ),
        _assistant_event(
            _text_block(),
            _tool_use("Bash", {"command": "pytest -q tests/unit/test_parsers.py"}, "t1"),
        ),
        _assistant_event(_tool_use("Read", {"file_path": "pipeline/parsers.py"}, "t2")),
        _assistant_event(
            _tool_use(
                "Edit",
                {
                    "file_path": "pipeline/parsers.py",
                    "old_string": "def parse(",
                    "new_string": "def parse_strict(",
                },
                "t3",
            )
        ),
        _assistant_event(_tool_use("Bash", {"command": "git diff --stat"}, "t4")),
        _assistant_event(
            _tool_use("Write", {"file_path": "pipeline/parsers.py", "content": "x"}, "t5")
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": _SUMMARY,
            }
        ),
    ]


def _write_realistic_agent_log(tmp_path):
    """Translate the realistic stream with ONE caller-owned state dict and
    write the canonical lines to <tmp_path>/wt/agent.log."""
    state = new_state()
    lines = []
    for raw in _realistic_stream_lines():
        lines.extend(translate_line(raw, state))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return worktree, log_path, state


# --------------------------------------------------------------- positive


def test_bash_tool_use_translates_to_the_canonical_step_line():
    state = new_state()
    raw = _assistant_event(_tool_use("Bash", {"command": "ls -la"}, "t1"))
    lines = _translate(raw, state)
    assert lines == ["[step 0] bash: ls -la"]
    assert lines == [format_step(0, "bash", "ls -la")]
    assert state["step"] == 1


def test_two_successive_tool_use_events_get_steps_0_then_1():
    state = new_state()
    first = _translate(
        _assistant_event(_tool_use("Bash", {"command": "echo one"}, "t1")), state
    )
    second = _translate(
        _assistant_event(_tool_use("Bash", {"command": "echo two"}, "t2")), state
    )
    assert first == ["[step 0] bash: echo one"]
    assert second == ["[step 1] bash: echo two"]
    assert state["step"] == 2


def test_one_message_with_two_tool_use_blocks_emits_two_consecutive_lines():
    state = new_state()
    raw = _assistant_event(
        _text_block("thinking out loud"),
        _tool_use("Bash", {"command": "cat a.txt"}, "t1"),
        _tool_use("Read", {"file_path": "b.txt"}, "t2"),
    )
    lines = _translate(raw, state)
    assert lines == ["[step 0] bash: cat a.txt", "[step 1] read: b.txt"]
    assert lines == [
        format_step(0, "bash", "cat a.txt"),
        format_step(1, "read", "b.txt"),
    ]
    assert state["step"] == 2


def test_result_event_emits_done_line_without_bumping_the_counter():
    state = new_state()
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All checks passed",
        }
    )
    lines = _translate(raw, state)
    assert lines == [format_done(0, "All checks passed")]
    assert DONE_MARKER in lines[0]
    assert state["step"] == 0  # only tool_use bumps the counter


def test_read_tool_use_line_matches_the_consumer_regex():
    state = new_state()
    raw = _assistant_event(_tool_use("Read", {"file_path": "/x/y.py"}, "t1"))
    lines = _translate(raw, state)
    assert len(lines) == 1
    assert re.match(r"^\[step \d+\] read: ", lines[0]), lines[0]


def test_edit_tool_uses_the_file_path_argument():
    state = new_state()
    raw = _assistant_event(
        _tool_use(
            "Edit",
            {"file_path": "pipeline/x.py", "old_string": "a", "new_string": "b"},
            "t1",
        )
    )
    assert _translate(raw, state) == ["[step 0] edit: pipeline/x.py"]


def test_file_tool_with_path_key_uses_the_path_argument():
    state = new_state()
    raw = _assistant_event(_tool_use("Write", {"path": "n.ipynb"}, "t1"))
    assert _translate(raw, state) == ["[step 0] write: n.ipynb"]


def test_unrecognised_input_shape_falls_back_to_compact_json():
    state = new_state()
    raw = _assistant_event(_tool_use("WebSearch", {"query": "flaky test"}, "t1"))
    lines = _translate(raw, state)
    assert len(lines) == 1
    match = STEP_LINE_RE.match(lines[0])
    assert match, lines[0]
    assert match.group(2) == "websearch"
    assert "query" in lines[0] and "flaky test" in lines[0]
    assert "\n" not in lines[0]  # compact: the arg stays on one line


def test_tool_name_is_lowercased_and_cleaned_for_the_consumer_regex():
    state = new_state()
    for name, expected in [
        ("Bash", "bash"),
        ("Read", "read"),
        ("Edit", "edit"),
        ("TodoWrite", "todowrite"),
        ("Web-Search", "web_search"),
        ("mcp__fs__read", "mcp__fs__read"),
    ]:
        raw = _assistant_event(_tool_use(name, {"command": "x"}, "t1"))
        lines = _translate(raw, state)
        assert len(lines) == 1, (name, lines)
        match = STEP_LINE_RE.match(lines[0])
        assert match, (name, lines[0])
        assert match.group(2) == expected, (name, lines[0])


def test_step_argument_is_truncated_like_format_step():
    state = new_state()
    command = "echo " + "x" * 500
    raw = _assistant_event(_tool_use("Bash", {"command": command}, "t1"))
    lines = _translate(raw, state)
    assert lines == [format_step(0, "bash", command)]  # format_step truncates
    assert len(lines[0]) == len("[step 0] bash: ") + 120


# --------------------------------------------------------------- boot


def test_system_init_emits_boot_line_with_model_and_session():
    state = new_state()
    raw = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-9f2c",
            "model": "claude-sonnet-4-5",
            "cwd": "/tmp/worktree",
        }
    )
    lines = _translate(raw, state)
    assert len(lines) == 1
    assert lines[0].startswith(BOOT_MARKER)
    assert "model=claude-sonnet-4-5" in lines[0]
    assert "sess-9f2c" in lines[0]
    assert state["step"] == 0  # boot does not bump the counter


def test_system_init_with_pid_and_model_renders_the_pid():
    state = new_state()
    raw = json.dumps(
        {"type": "system", "subtype": "init", "pid": 4242, "model": "claude-opus-4"}
    )
    lines = _translate(raw, state)
    assert len(lines) == 1
    assert lines[0].startswith(BOOT_MARKER)
    assert "pid=4242" in lines[0]
    assert "model=claude-opus-4" in lines[0]


def test_system_init_with_nothing_useful_emits_nothing():
    state = new_state()
    assert _translate('{"type": "system", "subtype": "init"}', state) == []
    assert (
        _translate(
            '{"type": "system", "subtype": "init", "model": null, "session_id": null}',
            state,
        )
        == []
    )
    assert state["step"] == 0


# --------------------------------------------------------------- negative


def test_non_json_line_passes_through_unchanged():
    state = new_state()
    raw = (
        "PIPELINE_TRANSPORT_NUM_CTX is set but the transport is not local; "
        "ignoring."
    )
    assert _translate(raw, state) == [raw]
    assert state["step"] == 0


def test_unparseable_brace_fragment_passes_through():
    state = new_state()
    assert _translate("{", state) == ["{"]


def test_empty_string_does_not_raise():
    state = new_state()
    result = _translate("", state)
    assert result in ([], [""])


def test_user_and_tool_result_types_emit_nothing():
    state = new_state()
    assert _translate('{"type": "user"}', state) == []
    assert (
        _translate(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "ok",
                            }
                        ]
                    },
                }
            ),
            state,
        )
        == []
    )
    assert _translate('{"type": "tool_result", "content": "ok"}', state) == []
    assert state["step"] == 0


def test_object_without_type_key_emits_nothing():
    state = new_state()
    assert _translate('{"cwd": "/tmp/worktree", "role": "assistant"}', state) == []


def test_text_only_assistant_message_emits_nothing():
    state = new_state()
    raw = _assistant_event(
        _text_block("I will now read the file."), _text_block("Then edit it.")
    )
    assert _translate(raw, state) == []
    assert state["step"] == 0


def test_assistant_content_null_or_missing_emits_nothing():
    state = new_state()
    assert _translate('{"type": "assistant", "message": {"content": null}}', state) == []
    assert _translate('{"type": "assistant", "message": {}}', state) == []
    assert _translate('{"type": "assistant"}', state) == []
    assert _translate('{"type": "assistant", "message": null}', state) == []
    assert state["step"] == 0


def test_tool_use_with_empty_input_still_matches_the_consumer_regex():
    state = new_state()
    raw = _assistant_event(_tool_use("Grep", {}, "t1"))
    lines = _translate(raw, state)
    assert len(lines) == 1
    match = STEP_LINE_RE.match(lines[0])
    assert match, lines[0]
    assert match.group(1) == "0"
    assert match.group(2) == "grep"


def test_valid_json_that_is_not_an_object_emits_nothing():
    state = new_state()
    for raw in ("null", "[]", "42", '"just a string"', "true"):
        assert _translate(raw, state) == []
    assert state["step"] == 0


_GARBAGE_LINES = [
    "null",
    "[]",
    '{"type": 5}',
    "",
    "   ",
    "42",
    '"just a string"',
    "not json at all",
    '{"type": "assistant"}',
    '{"type": "assistant", "message": {}}',
    '{"type": "assistant", "message": "not a dict"}',
    '{"type": "assistant", "message": 7}',
    '{"type": "assistant", "message": {"content": "plain string"}}',
    '{"type": "assistant", "message": {"content": [{}]}}',
    '{"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}',
    '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}',
    '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": 123, "input": null}]}}',
    '{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "not a dict"}]}}',
    '{"type": "result"}',
    '{"type": "result", "result": null}',
    '{"type": "system"}',
    '{"type": "system", "subtype": "other"}',
    '{"type": null}',
    '{"type": ["assistant"]}',
]


@pytest.mark.parametrize("raw", _GARBAGE_LINES)
def test_translate_line_never_raises(raw):
    state = new_state()
    out = _translate(raw, state)  # _translate asserts list-of-str, never None
    assert isinstance(out, list)


# --------------------------------------------------------------- state


def test_translate_line_initialises_a_missing_step_counter():
    state = {}  # caller-owned dict that does not carry the key yet
    raw = _assistant_event(_tool_use("Bash", {"command": "ls"}, "t1"))
    assert _translate(raw, state) == ["[step 0] bash: ls"]
    assert state["step"] == 1


def test_translator_is_stateless_across_independent_states():
    s1, s2 = {}, {}
    raw = _assistant_event(_tool_use("Bash", {"command": "ls"}, "t1"))
    assert _translate(raw, s1) == ["[step 0] bash: ls"]
    assert _translate(raw, s2) == ["[step 0] bash: ls"]
    assert s1["step"] == 1
    assert s2["step"] == 1


def test_new_state_returns_a_fresh_independent_counter():
    s1 = new_state()
    assert isinstance(s1, dict)
    assert s1 == {"step": 0}
    s2 = new_state()
    assert s2 == {"step": 0}
    assert s1 is not s2
    s1["step"] = 9
    assert new_state() == {"step": 0}  # later callers are unaffected
    assert s2 == {"step": 0}


# ------------------------------------------------- real-consumer integration


def test_translated_log_yields_rebrief_facts_including_last_attempt(tmp_path):
    worktree, _, _ = _write_realistic_agent_log(tmp_path)
    facts = _log_facts(worktree)
    assert facts, "the translated agent.log must yield rebrief facts"
    assert any("LAST ATTEMPT USED" in fact for fact in facts)
    assert any("LAST ATTEMPT USED 5 step(s)" in fact for fact in facts)
    # The used-tools fact lists the translator's emitted names; whether
    # rebrief counts any of them as "edit tools" is rebrief's own vocabulary
    # (_EDIT_TOOLS, out of scope for this module), so this test only asserts
    # the step accounting the translator itself controls.
    assert any("bash x2" in fact for fact in facts)
    assert not any("NEVER RAN THE TESTS" in fact for fact in facts)


def test_translated_log_satisfies_the_consumer_regexes_directly(tmp_path):
    _, log_path, _ = _write_realistic_agent_log(tmp_path)
    text = log_path.read_text(encoding="utf-8")
    assert BOOT_MARKER in text
    steps = STEP_LINE_RE.findall(text)
    assert [step for step, _ in steps] == ["0", "1", "2", "3", "4"]
    assert [tool for _, tool in steps] == ["bash", "read", "edit", "bash", "write"]
    commands = BASH_LINE_RE.findall(text)
    assert "pytest -q tests/unit/test_parsers.py" in commands
    assert "git diff --stat" in commands


def test_translated_log_yields_the_done_summary_for_build_detect(tmp_path):
    _, log_path, _ = _write_realistic_agent_log(tmp_path)
    assert _last_done_summary(log_path) == _SUMMARY


# --------------------------------------------------------------- purity


def test_module_defines_exactly_the_two_public_functions_and_no_class():
    import pipeline.claude_log_translate as mod

    own = {
        name: value
        for name, value in vars(mod).items()
        if getattr(value, "__module__", None) == mod.__name__
        and not name.startswith("_")
    }
    classes = [name for name, value in own.items() if inspect.isclass(value)]
    functions = sorted(
        name for name, value in own.items() if inspect.isfunction(value)
    )
    assert classes == []
    assert functions == ["new_state", "translate_line"]


def test_story_is_additive_no_existing_module_wires_the_translator():
    """No module other than the sanctioned wiring site may reference the
    translator. LOG-03 shipped it standalone; LOG-05 later wired it into
    ClaudeCliDriver.dispatch (app/backend_claude.py), which is the one
    allowed caller — every other module under pipeline/, app/, and
    scripts/ must still stay free of it."""
    root = Path(__file__).resolve().parents[2]
    module_itself = root / "pipeline" / "claude_log_translate.py"
    allowed_wiring = {root / "app" / "backend_claude.py"}
    offenders = []
    for dir_name in ("pipeline", "app", "scripts"):
        for path in sorted((root / dir_name).rglob("*.py")):
            if path == module_itself or path in allowed_wiring:
                continue  # the module may name itself; only dispatch may wire it
            if "claude_log_translate" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


# ------------------------------------------------- LOG-03 review hardening


def test_digit_tool_name_maps_into_the_consumer_grammar():
    """A digit-containing tool name must land in the consumer's [a-z_]+ run.

    The consumer regex ``^\\[step (\\d+)\\] ([a-z_]+):`` needs a run of
    ``[a-z_]`` immediately followed by ``:``; a surviving digit (``db2``)
    kills the match, so the step exists in agent.log but every downstream
    scan silently drops it.
    """
    state = new_state()
    raw = _assistant_event(_tool_use("mcp__db2__query", {"query": "SELECT 1"}, "t1"))
    lines = _translate(raw, state)
    assert len(lines) == 1, lines
    match = STEP_LINE_RE.match(lines[0])
    assert match, f"step line invisible to the consumer regex: {lines[0]!r}"
    assert re.fullmatch(r"[a-z_]+", match.group(2)), lines[0]
    assert state["step"] == 1  # bump-on-tool_use semantics unchanged


def test_digit_tool_name_step_is_recorded_by_the_real_consumer(tmp_path):
    state = new_state()
    raw = _assistant_event(_tool_use("mcp__db2__query", {"query": "SELECT 1"}, "t1"))
    lines = _translate(raw, state)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    facts = _log_facts(worktree)
    assert any("LAST ATTEMPT USED 1 step(s)" in fact for fact in facts), facts
    token = STEP_LINE_RE.match(lines[0]).group(2)
    assert any(f"{token} x1" in fact for fact in facts), facts


def test_digit_name_fix_leaves_the_counter_semantics_intact():
    """The 4-call trace: step 0, DONE 1, step 1, DONE 2 (0-based, as the
    committed suite pins: the first tool_use emits ``[step 0]``).

    Guards the two ways the digit fix could disturb the counter: a bump in
    the result branch (call 3 would emit [step 2]) and a reset/double-bump
    in the tool_use branch.
    """
    state = new_state()
    out: list[str] = []
    out += _translate(
        _assistant_event(_tool_use("mcp__db2__query", {"command": "SELECT 1"}, "t1")),
        state,
    )
    assert out == ["[step 0] mcp__db___query: SELECT 1"], out
    out += _translate(
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
        ),
        state,
    )
    assert out[1] == "[step 1] DONE: ok", out
    out += _translate(
        _assistant_event(
            _tool_use(
                "Edit",
                {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                "t2",
            )
        ),
        state,
    )
    assert out[2] == "[step 1] edit: a.py", out
    out += _translate(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
            }
        ),
        state,
    )
    assert out[3] == "[step 2] DONE: done", out
    assert state["step"] == 2


_ERROR_RESULTS = [
    '{"type": "result", "subtype": "error_max_turns", "is_error": true}',
    '{"type": "result", "subtype": "error_during_execution", "is_error": true, "result": ""}',
    '{"type": "result", "subtype": "error_during_execution", "is_error": true, "result": null}',
]


@pytest.mark.parametrize("raw", _ERROR_RESULTS)
def test_error_result_still_emits_one_marked_done_line(raw):
    state = new_state()
    lines = _translate(raw, state)
    assert len(lines) == 1, lines
    assert DONE_MARKER in lines[0], lines[0]
    summary = lines[0].split(DONE_MARKER, 1)[1].strip()
    assert summary, f"empty DONE summary: {lines[0]!r}"
    assert summary != "None", f"literal-None DONE summary: {lines[0]!r}"
    assert "error" in summary.lower(), f"unmarked error summary: {lines[0]!r}"
    assert state["step"] == 0  # DONE never bumps the counter


def test_error_result_done_summary_wins_over_a_stale_success(tmp_path):
    state = new_state()
    lines = []
    lines += _translate(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "stale success",
            }
        ),
        state,
    )
    lines += _translate(
        '{"type": "result", "subtype": "error_max_turns", "is_error": true}', state
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = _last_done_summary(log_path)
    assert summary, f"the '] DONE:' scan missed the log: {lines!r}"
    assert "error" in summary.lower(), summary


def test_tool_arg_newlines_collapse_to_one_physical_line(tmp_path):
    state = new_state()
    raw = _assistant_event(
        _tool_use("Bash", {"command": "echo one\necho two\r\necho three"}, "t1")
    )
    lines = _translate(raw, state)
    assert lines == ["[step 0] bash: echo one echo two echo three"], lines
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_done_summary_newlines_collapse_to_one_physical_line(tmp_path):
    state = new_state()
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "fixed it\nsecond line\r\nthird line",
        }
    )
    lines = _translate(raw, state)
    assert lines == ["[step 0] DONE: fixed it second line third line"], lines
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path = worktree / "agent.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert _last_done_summary(log_path) == "fixed it second line third line"