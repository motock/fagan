"""TDD tests for the provider usage adapters in ``pipeline.usage_record``.

Two provider transports hand back their token accounting in different shapes:
the Claude CLI ``--output-format json`` result object (``usage`` nested under a
``usage`` key, cost and duration as siblings) and the Ollama ``/api/chat``
response envelope (``prompt_eval_count`` / ``eval_count`` / ``total_duration``
in nanoseconds). These adapters are the single place that maps either shape
onto :func:`pipeline.usage_record.record_usage`, so extraction cannot drift
between the backends and the log translator that call them.

The invariants pinned here:

1. A well-formed payload records every token/cost/duration field, and only the
   Claude adapter sets ``served_model`` (from the payload's own ``model``).
2. Ollama durations arrive in nanoseconds and are recorded in milliseconds.
3. A payload whose ``usage`` is absent or not a dict still records a row with
   zero tokens -- the call happened, so the accounting line exists.
4. A non-dict argument records NOTHING and never raises: the adapter returns
   before touching ``record_usage``, so it cannot leave a zero-token row.
5. A missing ``total_duration`` records ``None``, never a fabricated ``0.0``.

``PLAN_DIR`` is the only seam: the fixture patches ``pipeline.persistence.PLAN_DIR``
at ``tmp_path`` (the Option B pattern the persistence module documents).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server is imported explicitly (the idiom in
# tests/unit/test_triage_cap_silent_skip.py): a new test file that imports a
# pipeline.* module settles the pre-existing import cycle by importing it first.
import pipeline.persistence
import pipeline.server
from pipeline import usage_record


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sidecar writer at a fresh directory."""
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _lines(path: Path) -> list[dict]:
    """Parse the JSONL sidecar at *path* (empty list when absent)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_claude_result_records_tokens_cost_duration_and_served_model(plan_dir: Path) -> None:
    payload = {
        "model": "claude-sonnet-4",
        "usage": {
            "input_tokens": 11,
            "output_tokens": 4,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 200,
        },
        "total_cost_usd": 0.02,
        "duration_ms": 1500,
    }

    with usage_record.usage_scope("plan-a", "story-1", attempt=1):
        usage_record.record_claude_result(payload, model="anthropic/claude", role="dispatch")

    records = _lines(plan_dir / "plan-a.usage.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["backend"] == "claude"
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 4
    assert record["cache_creation_input_tokens"] == 30
    assert record["cache_read_input_tokens"] == 200
    assert record["cost_usd"] == pytest.approx(0.02)
    assert record["duration_ms"] == 1500
    assert record["served_model"] == "claude-sonnet-4"


def test_ollama_envelope_records_counts_and_nanoseconds_as_milliseconds(plan_dir: Path) -> None:
    envelope = {"prompt_eval_count": 7, "eval_count": 3, "total_duration": 2_000_000}

    with usage_record.usage_scope("plan-b", "story-2", attempt=1):
        usage_record.record_ollama_envelope(envelope, model="qwen2.5:7b", role="dispatch")

    records = _lines(plan_dir / "plan-b.usage.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["backend"] == "ollama"
    assert record["input_tokens"] == 7
    assert record["output_tokens"] == 3
    assert record["duration_ms"] == 2.0


def test_claude_result_without_usable_usage_still_records_zero_tokens(plan_dir: Path) -> None:
    with usage_record.usage_scope("plan-c", "story-3", attempt=1):
        usage_record.record_claude_result(
            {"model": "claude-sonnet-4", "duration_ms": 1200},
            model="anthropic/claude",
            role="dispatch",
        )
        usage_record.record_claude_result(
            {"usage": [{"input_tokens": 5}], "model": "claude-sonnet-4"},
            model="anthropic/claude",
            role="dispatch",
        )

    records = _lines(plan_dir / "plan-c.usage.jsonl")
    assert len(records) == 2
    assert records[0]["input_tokens"] == 0
    assert records[0]["output_tokens"] == 0
    assert records[0]["duration_ms"] == 1200
    assert records[1]["input_tokens"] == 0
    assert records[1]["output_tokens"] == 0


def test_non_dict_arguments_record_nothing_and_do_not_raise(plan_dir: Path) -> None:
    scope = {"plan": "plan-d", "story_key": "story-4", "attempt": 1}

    usage_record.record_claude_result(
        "not a dict", model="claude-sonnet-4", role="dispatch", scope=scope
    )
    usage_record.record_ollama_envelope(None, model="qwen2.5:7b", role="dispatch", scope=scope)

    # Neither no-op may leave a row behind: a zero-token row here would mean the
    # adapter reached record_usage instead of returning early.
    assert _lines(plan_dir / "plan-d.usage.jsonl") == []

    usage_record.record_ollama_envelope(
        {
            "model": "qwen2.5:7b",
            "prompt_eval_count": 7,
            "eval_count": 3,
            "total_duration": 2_000_000,
        },
        model="qwen2.5:7b",
        role="dispatch",
        scope=scope,
    )

    records = _lines(plan_dir / "plan-d.usage.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["input_tokens"] == 7
    assert record["output_tokens"] == 3
    assert record["duration_ms"] == 2.0
    assert record["backend"] == "ollama"
    assert record["served_model"] is None


def test_ollama_envelope_without_total_duration_records_none(plan_dir: Path) -> None:
    with usage_record.usage_scope("plan-e", "story-5", attempt=1):
        usage_record.record_ollama_envelope(
            {"prompt_eval_count": 2, "eval_count": 1}, model="qwen2.5:7b", role="dispatch"
        )

    records = _lines(plan_dir / "plan-e.usage.jsonl")
    assert len(records) == 1
    assert records[0]["duration_ms"] is None
    assert records[0]["input_tokens"] == 2
    assert records[0]["output_tokens"] == 1
