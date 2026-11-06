"""The Claude CLI implementer's stream-json ``result`` event feeds usage accounting.

A dispatched Claude implementation streams NDJSON through the ``line_filter``
that ``ClaudeCliDriver.dispatch`` installs, on a drain thread. The filter hands
every ``result`` event to ``usage_record.record_claude_result`` so the
implementer's tokens and cost land in the plan's usage sidecar with role
``dispatch`` — the same accounting live reviews already get with role
``review``.

Two seams only: ``pipeline.execution.spawn_harness`` is the external boundary
(it is faked to capture the ``line_filter`` dispatch installs, so no real
``claude`` process is spawned), and ``pipeline.persistence.PLAN_DIR`` redirects
the usage sidecar into ``tmp_path``.

The scope is captured at dispatch time and closed over by the filter, because
the drain thread does not inherit the context variable that binds it; the
filters below are therefore always exercised *outside* the scope block.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# pipeline.server first: every pipeline.* module is imported after it (the
# repo's cold-import idiom, see tests/unit/test_triage_cap_silent_skip.py).
import app.backend_claude as bc
import pipeline.persistence

# pipeline.server is imported before the remaining pipeline.* modules: the
# repo's cold-import idiom (see tests/unit/test_triage_cap_silent_skip.py).
import pipeline.server
from pipeline import claude_log_translate, execution, usage_record

MODEL = "deepseek-v4.1-flash:cloud"
PLAN = "demo-plan"
STORY = "S-1"


def _result_line(
    input_tokens: int = 1200,
    output_tokens: int = 300,
    *,
    with_usage: bool = True,
) -> str:
    """One stream-json ``result`` event line as the CLI writes it."""
    payload: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 4200,
        "total_cost_usd": 0.0123,
        "result": "implemented the story",
    }
    if with_usage:
        payload["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
        }
    return json.dumps(payload)


def _assistant_line() -> str:
    return json.dumps({"type": "assistant", "message": {"content": []}})


def _usage_records(plan_dir: Path) -> list[dict]:
    """Parsed lines of the plan's usage sidecar (empty when never written)."""
    path = plan_dir / f"{PLAN}.usage.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _capture_dispatch_line_filter(
    monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> callable:
    """Dispatch through a faked harness spawn and return the installed filter."""
    captured: dict = {}

    def _fake_spawn_harness(cmd, *, cwd, log_path, append, env=None, line_filter=None):
        captured["cmd"] = list(cmd)
        captured["line_filter"] = line_filter
        return SimpleNamespace()

    monkeypatch.setattr(execution, "spawn_harness", _fake_spawn_harness)
    # Pin the harness selection so an operator's PIPELINE_AGENT_HARNESS cannot
    # redirect this test onto the aider path.
    monkeypatch.setattr(bc, "resolve_harness_name", lambda default: "claude")
    monkeypatch.setattr(bc, "_first_party_claude_env", dict)

    handle = bc.ClaudeCliDriver().dispatch(
        "implement the story",
        model=MODEL,
        cwd=work_dir,
        log_path=work_dir / "agent.log",
        append=False,
    )
    assert handle.model == MODEL
    line_filter = captured["line_filter"]
    assert line_filter is not None
    return line_filter


def test_result_line_keeps_parsed_object_in_state_and_returns_done() -> None:
    """The translate session now carries the result object for the caller."""
    state = claude_log_translate.new_state()

    lines = claude_log_translate.translate_line(_result_line(), state)

    assert state["result"]["type"] == "result"
    assert state["result"]["usage"]["input_tokens"] == 1200
    assert any("DONE" in line for line in lines)


def test_non_result_lines_add_no_result_key() -> None:
    """assistant/user events must not plant a stale result for a later pop."""
    state = claude_log_translate.new_state()

    claude_log_translate.translate_line(_assistant_line(), state)
    assert "result" not in state

    claude_log_translate.translate_line(json.dumps({"type": "user"}), state)
    assert "result" not in state


def test_dispatch_filter_records_usage_for_a_result_event(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scope bound at dispatch time is recorded under that plan."""
    with usage_record.usage_scope(PLAN, STORY):
        line_filter = _capture_dispatch_line_filter(monkeypatch, plan_dir)

    lines = line_filter(_result_line(1200, 300))

    records = _usage_records(plan_dir)
    assert len(records) == 1
    record = records[0]
    assert record["role"] == "dispatch"
    assert record["backend"] == "claude"
    assert record["model"] == MODEL
    assert record["plan"] == PLAN
    assert record["input_tokens"] == 1200
    assert record["output_tokens"] == 300
    assert any("DONE" in line for line in lines)


def test_no_scope_bound_writes_no_usage_but_still_returns_done(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattributable implementer call records nothing and never breaks."""
    assert usage_record.current_scope() is None

    line_filter = _capture_dispatch_line_filter(monkeypatch, plan_dir)

    lines = line_filter(_result_line())

    assert any("DONE" in line for line in lines)
    assert list(plan_dir.glob("*.usage.jsonl")) == []


def test_result_event_without_usage_records_zero_tokens(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result event with no ``usage`` block is accounted as zero, not fatal."""
    with usage_record.usage_scope(PLAN, STORY):
        line_filter = _capture_dispatch_line_filter(monkeypatch, plan_dir)

    lines = line_filter(_result_line(with_usage=False))

    records = _usage_records(plan_dir)
    assert len(records) == 1
    assert records[0]["input_tokens"] == 0
    assert records[0]["output_tokens"] == 0
    assert any("DONE" in line for line in lines)


def test_two_result_events_record_two_usage_records(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed stream's second result is accounted, not collapsed into the first."""
    with usage_record.usage_scope(PLAN, STORY):
        line_filter = _capture_dispatch_line_filter(monkeypatch, plan_dir)

    line_filter(_assistant_line())
    line_filter(_result_line(1200, 300))
    line_filter(_assistant_line())
    line_filter(_result_line(7, 3))

    records = _usage_records(plan_dir)
    assert len(records) == 2
    assert [record["input_tokens"] for record in records] == [1200, 7]
    assert [record["output_tokens"] for record in records] == [300, 3]
