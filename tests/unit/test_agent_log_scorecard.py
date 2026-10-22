"""Tests for the per-run agent.log scorecard (Mode 36, deferred item 7)."""

from __future__ import annotations

import json

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
