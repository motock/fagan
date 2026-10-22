"""Tests for the per-run agent.log scorecard (Mode 36, deferred item 7)."""

from __future__ import annotations

import json

import pytest

from pipeline.agent_log_format import format_done
from scripts import agent_log_scorecard as s

BOOT = (
    "[boot] pid=123 model=gpt-oss:20b-high:latest "
    "endpoint=http://localhost:11434 provider=ollama steps=120 timeout=5400.0s"
)


def _log(*lines: str) -> str:
    """Join log lines into one agent.log body."""
    return "\n".join(lines) + "\n"


def test_scorecard_reports_steps_tools_first_mutation_and_ratio() -> None:
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 1] bash: ls",
            "[step 2] bash: pwd",
            "[step 3] str_replace: a.py",
            "[step 4] view_file: a.py",
            "[step 5] replace_lines: b.py",
            "[step 5] view_file: b.py",
        )
    )

    assert score["steps"] == 5
    assert score["tool_counts"] == {
        "bash": 2,
        "replace_lines": 1,
        "str_replace": 1,
        "view_file": 2,
    }
    assert score["mutating_tool_counts"] == {
        "replace_lines": 1,
        "str_replace": 1,
    }
    assert score["first_mutation_step"] == 3
    assert score["mutation_steps"] == 2
    assert score["productive_ratio"] == 0.4
    assert score["outcome"] == "unknown"


def test_boot_fields_are_typed() -> None:
    score = s.parse_agent_log(_log(BOOT))

    assert score["boot"]["steps"] == 120
    assert isinstance(score["boot"]["steps"], int)
    assert score["boot"]["timeout"] == 5400.0
    assert isinstance(score["boot"]["timeout"], float)
    assert score["boot"]["model"] == "gpt-oss:20b-high:latest"
    assert isinstance(score["boot"]["model"], str)


def test_done_outcome_wins_over_earlier_guard_trips() -> None:
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 1] view_file: a.py",
            "    [read-heavy nudge]",
            "[step 2] DONE: shipped the fix",
        )
    )

    assert score["outcome"] == "done"
    assert score["done_summary"] == "shipped the fix"
    assert score["guard_trips"] == {"read-heavy nudge": 1}


def test_parking_line_sets_parked_outcome_and_reason() -> None:
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 1] view_file: a.py",
            "[parking: read-heavy after nudge]",
        )
    )

    assert score["outcome"] == "parked"
    assert "parking: read-heavy after nudge" in score["parked_reason"]


def test_indented_parking_line_is_not_counted_as_a_guard_trip() -> None:
    """Real parking lines are 3-space indented, so they also match NUDGE_LINE_RE.

    Regression guard: the parking check must win over the nudge branch, or a
    run that parked reports ``unknown`` and a bogus "parking" guard trip.
    """
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 1] view_file: a.py",
            "   [parking: read-heavy after nudge]",
        )
    )

    assert score["outcome"] == "parked"
    assert score["parked_reason"] == "[parking: read-heavy after nudge]"
    assert score["guard_trips"] == {}


def test_guard_message_step_numbers_count_toward_steps() -> None:
    """Guard/outcome lines carry a step prefix but no ``tool:`` shape.

    Regression guard: only ``[step N] tool: arg`` and DONE lines used to advance
    the step count, so a run that parked at step 4 reported steps=2 and an
    understated productive_ratio.
    """
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 2] view_file: a.py",
            "[step 4] no successful edit in 30 steps "
            "(last progress at step 2); parking",
        )
    )

    assert score["steps"] == 4
    assert score["outcome"] == "parked"
    assert "no successful edit" in score["parked_reason"]


def test_done_summary_strips_a_trailing_correlation_id() -> None:
    score = s.parse_agent_log(_log("[step 3] DONE: shipped it [cid=abc123]"))

    assert score["done_summary"] == "shipped it"
    assert score["correlation_id"] == "abc123"


@pytest.mark.parametrize(
    "reason",
    ["read-heavy", "repetition", "churn", "str_replace-fail", "off-task-drift"],
)
def test_indented_parking_marker_sets_parked_and_is_not_a_guard_trip(
    reason: str,
) -> None:
    """The real parking lines are 3-space indented, so they match NUDGE_LINE_RE.

    Regression guard: the nudge branch used to consume them first, recording a
    bogus "parking" guard trip and losing the parked outcome.
    """
    score = s.parse_agent_log(_log(f"   [parking: {reason}]"))

    assert score["outcome"] == "parked"
    assert reason in score["parked_reason"]
    assert "parking" not in score["guard_trips"]
    assert sum(score["guard_trips"].values()) == 0


@pytest.mark.parametrize("done_line", ["DONE: completed", "[step 5] DONE: completed"])
def test_parked_outcome_survives_a_following_done_line(done_line: str) -> None:
    score = s.parse_agent_log(_log("   [parking: read-heavy]", done_line))

    assert score["parked_reason"] == "[parking: read-heavy]"
    assert score["guard_trips"] == {}
    assert score["outcome"] == "parked"


def test_done_summary_strips_the_cid_that_format_done_appends() -> None:
    done_line = format_done(3, "completed", correlation_id="deadbeef")

    score = s.parse_agent_log(_log(done_line))

    assert score["correlation_id"] == "deadbeef"
    assert score["done_summary"] == "completed"
    assert "[cid=deadbeef]" not in score["done_summary"]


def test_guard_trip_labels_group_across_digit_differences() -> None:
    score = s.parse_agent_log(
        _log(
            BOOT,
            "[step 1] view_file: a.py",
            "    [no scratchpad update in 15 steps; nudging]",
            "    [no scratchpad update in 30 steps; nudging]",
            "    [off-task nudge: tmp/a.txt not in assigned scope]",
        )
    )

    assert score["guard_trips"] == {
        "no scratchpad update in N steps": 2,
        "off-task nudge": 1,
    }


def test_malformed_and_unrelated_lines_are_ignored() -> None:
    score = s.parse_agent_log(
        _log(
            "[step x] view_file: a.py",
            "the agent said something in prose",
            "[step 7] NotATool: a.py",
            "    an indented line with no brackets",
            "[not closed",
        )
    )

    assert score["steps"] == 0
    assert score["tool_counts"] == {}
    assert score["guard_trips"] == {}
    assert score["outcome"] == "unknown"


def test_empty_log_scores_zero_and_unknown() -> None:
    score = s.parse_agent_log("")

    assert score["steps"] == 0
    assert score["first_mutation_step"] is None
    assert score["productive_ratio"] == 0.0
    assert score["outcome"] == "unknown"
    assert score["boot"] is None


def test_main_returns_2_for_a_missing_file(tmp_path, capsys) -> None:
    assert s.main([str(tmp_path / "nope.log")]) == 2
    assert "no agent.log" in capsys.readouterr().err


def test_main_json_output_is_parseable(tmp_path, capsys) -> None:
    log = tmp_path / "agent.log"
    log.write_text(
        _log(
            "[step 1] create_file: tmp/a.txt",
            "[step 2] DONE: completed",
        ),
        encoding="utf-8",
    )

    assert s.main([str(log), "--json"]) == 0
    score = json.loads(capsys.readouterr().out)
    assert score["first_mutation_step"] == 1
    assert score["outcome"] == "done"
