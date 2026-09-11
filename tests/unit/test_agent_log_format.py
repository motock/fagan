"""Tests for pipeline/agent_log_format.py — the agent.log line grammar.

This module is the single source of truth for the ``[boot]``, ``[step N]
<tool>: <arg>`` and ``[step N] DONE: <summary>`` line shapes, so a second
producer (the Claude translator, a later story) can emit byte-identical
lines. The contract asserted here is the one the EXISTING consumers already
rely on:

- pipeline/rebrief.py ``_log_facts``:
    ``r"^\\[step (\\d+)\\] ([a-z_]+):"`` and
    ``r"^\\[step \\d+\\] bash: (.*)$"`` (both re.MULTILINE)
- pipeline/build_detect.py ``_last_done_summary``: literal ``"] DONE:"``
- pipeline/rebrief.py ``_current_attempt_log``: literal ``"[boot]"``

Written test-first: this file stays RED (ModuleNotFoundError on
``pipeline.agent_log_format``) until that module exists with
``format_boot`` / ``format_step`` / ``format_done``.
"""

import inspect
import re
from pathlib import Path

import pytest

from pipeline import agent_log_format

# The real consumer regexes, re-declared verbatim from the source. If a
# consumer's regex ever changes, update these in the same commit.
STEP_TOOL_RE = r"^\[step (\d+)\] ([a-z_]+):"
STEP_BASH_RE = r"^\[step \d+\] bash: (.*)$"

_EMPTY = inspect.Parameter.empty


# ---------------------------------------------------------------------------
# module shape: exactly the three functions, no classes, __all__ membership
# ---------------------------------------------------------------------------


def test_module_is_the_agreed_file():
    assert Path(agent_log_format.__file__).name == "agent_log_format.py"


def test_module_exports_the_three_formatters():
    for name in ("format_boot", "format_step", "format_done"):
        assert name in agent_log_format.__all__  # membership, NOT equality
        assert callable(getattr(agent_log_format, name)), name


def test_module_defines_no_classes():
    defined = [
        name
        for name, value in vars(agent_log_format).items()
        if inspect.isclass(value) and value.__module__ == agent_log_format.__name__
    ]
    assert defined == []


def test_format_boot_signature_matches_the_brief():
    params = inspect.signature(agent_log_format.format_boot).parameters
    assert [(p.name, p.kind, p.default) for p in params.values()] == [
        ("pid", inspect.Parameter.KEYWORD_ONLY, _EMPTY),
        ("model", inspect.Parameter.KEYWORD_ONLY, _EMPTY),
        ("endpoint", inspect.Parameter.KEYWORD_ONLY, ""),
        ("provider", inspect.Parameter.KEYWORD_ONLY, ""),
        ("steps", inspect.Parameter.KEYWORD_ONLY, None),
        ("timeout", inspect.Parameter.KEYWORD_ONLY, None),
    ]


def test_format_step_signature_matches_the_brief():
    params = inspect.signature(agent_log_format.format_step).parameters
    assert [(p.name, p.kind, p.default) for p in params.values()] == [
        ("step", inspect.Parameter.POSITIONAL_OR_KEYWORD, _EMPTY),
        ("tool", inspect.Parameter.POSITIONAL_OR_KEYWORD, _EMPTY),
        ("arg", inspect.Parameter.POSITIONAL_OR_KEYWORD, ""),
        ("correlation_id", inspect.Parameter.KEYWORD_ONLY, ""),
    ]


def test_format_done_signature_matches_the_brief():
    params = inspect.signature(agent_log_format.format_done).parameters
    assert [(p.name, p.kind, p.default) for p in params.values()] == [
        ("step", inspect.Parameter.POSITIONAL_OR_KEYWORD, _EMPTY),
        ("summary", inspect.Parameter.POSITIONAL_OR_KEYWORD, _EMPTY),
        ("correlation_id", inspect.Parameter.KEYWORD_ONLY, ""),
    ]


# ---------------------------------------------------------------------------
# positive: the exact byte shapes
# ---------------------------------------------------------------------------


def test_format_step_basic():
    assert agent_log_format.format_step(3, "bash", "ls -la") == "[step 3] bash: ls -la"


def test_format_step_zero_and_underscore_tool():
    assert (
        agent_log_format.format_step(0, "create_file", ".agent_scratchpad.md")
        == "[step 0] create_file: .agent_scratchpad.md"
    )


def test_format_done_basic():
    assert (
        agent_log_format.format_done(7, "added the thing")
        == "[step 7] DONE: added the thing"
    )


def test_format_step_cid_suffix():
    line = agent_log_format.format_step(1, "bash", "ls", correlation_id="abc")
    assert line == "[step 1] bash: ls [cid=abc]"
    assert line.endswith(" [cid=abc]")


def test_format_done_cid_suffix():
    line = agent_log_format.format_done(7, "added the thing", correlation_id="abc")
    assert line == "[step 7] DONE: added the thing [cid=abc]"
    assert line.endswith(" [cid=abc]")


def test_format_boot_full():
    assert (
        agent_log_format.format_boot(
            pid=123,
            model="glm",
            endpoint="http://localhost:11434",
            provider="ollama",
            steps=120,
            timeout=8100.0,
        )
        == "[boot] pid=123 model=glm endpoint=http://localhost:11434 "
        "provider=ollama steps=120 timeout=8100.0s"
    )


# ---------------------------------------------------------------------------
# contract: the lines satisfy the REAL consumer regexes / markers
# ---------------------------------------------------------------------------


def test_step_line_matches_consumer_tool_regex():
    match = re.match(STEP_TOOL_RE, agent_log_format.format_step(2, "bash", "echo hi"))
    assert match is not None
    assert match.groups() == ("2", "bash")


def test_bash_line_matches_consumer_bash_regex():
    line = agent_log_format.format_step(2, "bash", "echo hi")
    assert re.findall(STEP_BASH_RE, line, re.MULTILINE) == ["echo hi"]


def test_done_line_contains_consumer_done_marker():
    assert "] DONE:" in agent_log_format.format_done(1, "x")


def test_boot_line_contains_consumer_boot_marker():
    assert "[boot]" in agent_log_format.format_boot(pid=1, model="m")


def test_multiline_log_of_formatted_lines_parses_like_a_real_log():
    """A log assembled from the formatters must feed _log_facts' findall calls
    exactly as today's producer output does."""
    log = "\n".join(
        [
            agent_log_format.format_boot(pid=9, model="m"),
            agent_log_format.format_step(0, "bash", "pwd"),
            agent_log_format.format_step(1, "create_file", "x.py"),
            agent_log_format.format_done(2, "did it"),
        ]
    )
    assert re.findall(STEP_TOOL_RE, log, re.MULTILINE) == [
        ("0", "bash"),
        ("1", "create_file"),
    ]
    assert re.findall(STEP_BASH_RE, log, re.MULTILINE) == ["pwd"]


# ---------------------------------------------------------------------------
# negative / boundary
# ---------------------------------------------------------------------------


def test_arg_longer_than_120_is_truncated_to_exactly_120():
    arg = "a" * 500
    line = agent_log_format.format_step(4, "bash", arg)
    assert line == f"[step 4] bash: {'a' * 120}"
    assert len(line) == len("[step 4] bash: ") + 120


def test_arg_of_exactly_120_is_not_truncated():
    arg = "b" * 120
    assert agent_log_format.format_step(4, "bash", arg) == f"[step 4] bash: {arg}"


def test_arg_of_121_keeps_only_the_first_120_characters():
    arg = "c" * 121
    line = agent_log_format.format_step(4, "bash", arg)
    assert line == f"[step 4] bash: {'c' * 120}"


def test_truncation_applies_before_the_cid_suffix():
    arg = "d" * 200
    line = agent_log_format.format_step(4, "bash", arg, correlation_id="x")
    assert line == f"[step 4] bash: {'d' * 120} [cid=x]"


def test_step_without_arg_keeps_trailing_colon_space_and_matches_regex():
    line = agent_log_format.format_step(0, "bash")
    assert line == "[step 0] bash: "
    assert re.match(STEP_TOOL_RE, line) is not None


def test_empty_correlation_id_adds_no_cid_suffix():
    line = agent_log_format.format_step(2, "bash", "echo hi", correlation_id="")
    assert "[cid=" not in line
    assert line == "[step 2] bash: echo hi"
    done = agent_log_format.format_done(2, "ok", correlation_id="")
    assert "[cid=" not in done
    assert done == "[step 2] DONE: ok"


def test_empty_summary_keeps_the_done_marker_shape():
    line = agent_log_format.format_done(1, "")
    assert line == "[step 1] DONE: "
    assert "] DONE:" in line


def test_no_formatter_output_contains_a_newline():
    outputs = [
        agent_log_format.format_boot(pid=1, model="m"),
        agent_log_format.format_boot(
            pid=1, model="m", endpoint="e", provider="p", steps=2, timeout=3.0
        ),
        agent_log_format.format_step(1, "bash", "ls"),
        agent_log_format.format_step(1, "bash", "ls", correlation_id="c"),
        agent_log_format.format_step(0, "bash"),
        agent_log_format.format_done(1, "s"),
        agent_log_format.format_done(1, "s", correlation_id="c"),
    ]
    for result in outputs:
        assert "\n" not in result
        assert not result.endswith("\n")


def test_boot_omits_unsupplied_fields_entirely():
    assert agent_log_format.format_boot(pid=1, model="m") == "[boot] pid=1 model=m"


def test_boot_skips_explicit_empty_strings_and_none():
    line = agent_log_format.format_boot(
        pid=1, model="m", endpoint="", provider="", steps=None, timeout=None
    )
    assert line == "[boot] pid=1 model=m"


def test_boot_keeps_field_order_with_a_subset_of_fields():
    line = agent_log_format.format_boot(pid=1, model="m", steps=5, timeout=60.0)
    assert line == "[boot] pid=1 model=m steps=5 timeout=60.0s"


def test_boot_zero_values_are_supplied_so_they_must_render():
    # steps=0 / timeout=0.0 are falsy but were SUPPLIED - "skip" means
    # empty-string-or-None only, never a falsy number.
    assert (
        agent_log_format.format_boot(pid=1, model="m", steps=0)
        == "[boot] pid=1 model=m steps=0"
    )
    assert (
        agent_log_format.format_boot(pid=1, model="m", timeout=0.0)
        == "[boot] pid=1 model=m timeout=0.0s"
    )


def test_boot_timeout_renders_with_trailing_s():
    line = agent_log_format.format_boot(pid=1, model="m", timeout=8100.0)
    assert line.endswith("timeout=8100.0s")


# ---------------------------------------------------------------------------
# missing required fields / keyword-only enforcement
# ---------------------------------------------------------------------------


def test_format_step_requires_step_and_tool():
    with pytest.raises(TypeError) as excinfo:
        agent_log_format.format_step()
    message = str(excinfo.value)
    assert "missing" in message
    assert "step" in message
    assert "tool" in message


def test_format_step_tool_is_required_even_with_step_given():
    with pytest.raises(TypeError):
        agent_log_format.format_step(1)


def test_format_done_requires_step_and_summary():
    with pytest.raises(TypeError):
        agent_log_format.format_done()


def test_format_boot_requires_pid_and_model():
    with pytest.raises(TypeError) as excinfo:
        agent_log_format.format_boot()
    message = str(excinfo.value)
    assert "pid" in message
    assert "model" in message


def test_format_boot_pid_and_model_are_keyword_only():
    with pytest.raises(TypeError):
        agent_log_format.format_boot(1, "m")


def test_correlation_id_is_keyword_only_on_both_formatters():
    with pytest.raises(TypeError):
        agent_log_format.format_step(1, "bash", "ls", "abc")
    with pytest.raises(TypeError):
        agent_log_format.format_done(1, "ok", "abc")