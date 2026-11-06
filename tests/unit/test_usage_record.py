"""TDD tests for the scoped usage record sidecar (``pipeline.usage_record``).

The usage record is a per-plan JSONL sidecar with a FIXED field set: one line
per billable provider call, carrying which plan/story/attempt/correlation id the
call happened under and what it cost. These tests pin four invariants:

1. The scope is a ``ContextVar``: nesting binds and *restores* the outer scope
   (``reset(token)``, not ``set(None)``), and ``current_scope`` hands out copies
   so a caller cannot mutate the live binding.
2. ``record_usage`` writes nothing at all when no scope is bound -- a call made
   outside a story scope is not attributable and must not create a file.
3. Boundary values from provider responses are sanitized rather than trusted:
   non-int/negative token counts become 0, non-numeric/negative money and
   duration become ``None``, while a legitimate ``0`` survives.
4. ``record_usage`` never raises: a failing write is dropped with a warning and
   leaves the bound scope intact for the next call.

``PLAN_DIR`` is the only seam: tests patch ``pipeline.persistence.PLAN_DIR`` at
``tmp_path`` (the Option B pattern the persistence module documents).
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
from pipeline.usage_record import (
    USAGE_FIELDS,
    current_scope,
    is_recording,
    record_usage,
    scoped_story_call,
    usage_scope,
)


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


def test_bound_scope_writes_one_record_with_the_fixed_field_set(plan_dir: Path) -> None:
    with usage_scope("plan-a", "story-1", attempt=3):
        assert is_recording() is True
        record_usage(
            role="dispatch",
            backend="claude",
            model="anthropic/claude-sonnet",
            served_model="claude-sonnet-4",
            input_tokens=1200,
            output_tokens=340,
            cache_creation_input_tokens=11,
            cache_read_input_tokens=22,
            cost_usd=0.0421,
            duration_ms=9876,
        )

    records = _lines(plan_dir / "plan-a.usage.jsonl")
    assert len(records) == 1
    record = records[0]
    assert set(record.keys()) == set(USAGE_FIELDS)
    assert record["plan"] == "plan-a"
    assert record["story_key"] == "story-1"
    assert record["attempt"] == 3
    assert record["role"] == "dispatch"
    assert record["backend"] == "claude"
    assert record["model"] == "anthropic/claude-sonnet"
    assert record["served_model"] == "claude-sonnet-4"
    assert record["input_tokens"] == 1200
    assert record["output_tokens"] == 340
    assert record["cache_creation_input_tokens"] == 11
    assert record["cache_read_input_tokens"] == 22
    assert record["cost_usd"] == pytest.approx(0.0421)
    assert record["duration_ms"] == 9876
    assert isinstance(record["ts"], str) and record["ts"]


def test_no_scope_bound_and_no_explicit_scope_writes_nothing(plan_dir: Path) -> None:
    assert current_scope() is None
    assert is_recording() is False

    record_usage(role="dispatch", backend="claude", model="m", input_tokens=5)

    assert list(plan_dir.iterdir()) == []


def test_token_and_cost_boundary_values_are_sanitized(plan_dir: Path) -> None:
    with usage_scope("plan-b", "story-2", attempt=1):
        record_usage(role="dispatch", backend="claude", model="m", input_tokens="12")
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=-5)
        # bool is not a token count here, even though isinstance(True, int)
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=True)
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=None)
        record_usage(role="dispatch", backend="claude", model="m", cost_usd=-1)
        record_usage(role="dispatch", backend="claude", model="m", cost_usd="x")

    records = _lines(plan_dir / "plan-b.usage.jsonl")
    assert len(records) == 6
    assert [r["input_tokens"] for r in records[:4]] == [0, 0, 0, 0]
    assert records[4]["cost_usd"] is None
    assert records[5]["cost_usd"] is None


def test_legitimate_zero_values_survive_and_two_records_append_in_order(plan_dir: Path) -> None:
    with usage_scope("plan-c", "story-3", attempt=2):
        record_usage(
            role="dispatch",
            backend="claude",
            model="m",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0,
            duration_ms=0,
        )
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=7)

    records = _lines(plan_dir / "plan-c.usage.jsonl")
    assert len(records) == 2
    assert records[0]["input_tokens"] == 0
    assert records[0]["output_tokens"] == 0
    assert records[0]["cost_usd"] == 0
    assert records[0]["duration_ms"] == 0
    assert records[1]["input_tokens"] == 7


def test_explicit_scope_records_without_and_wins_over_a_bound_scope(plan_dir: Path) -> None:
    explicit = {"plan": "drained", "story_key": "s-drained", "attempt": 9}

    # No scope bound at all: this is how a drain thread records a finished call.
    record_usage(role="dispatch", backend="claude", model="m", input_tokens=3, scope=explicit)
    assert _lines(plan_dir / "drained.usage.jsonl")[0]["attempt"] == 9

    with usage_scope("outer", "s-outer", attempt=1):
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=4, scope=explicit)

    drained = _lines(plan_dir / "drained.usage.jsonl")
    assert len(drained) == 2
    assert [r["attempt"] for r in drained] == [9, 9]
    assert not (plan_dir / "outer.usage.jsonl").exists()


def test_unwritable_plan_dir_does_not_raise_and_keeps_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocking_file = tmp_path / "notadir"
    blocking_file.write_text("not a directory")
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", blocking_file)

    with usage_scope("plan-d", "story-4", attempt=1):
        assert record_usage(role="dispatch", backend="claude", model="m", input_tokens=1) is None
        assert current_scope() == {"plan": "plan-d", "story_key": "story-4", "attempt": 1}

        # The failed write must not unbind or corrupt the scope.
        monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=2)

    records = _lines(tmp_path / "plan-d.usage.jsonl")
    assert len(records) == 1
    assert records[0]["input_tokens"] == 2
    assert records[0]["attempt"] == 1


def test_nested_scope_restores_the_outer_scope(plan_dir: Path) -> None:
    with usage_scope("p", "s", attempt=1):
        assert current_scope() == {"plan": "p", "story_key": "s", "attempt": 1}

        with usage_scope("p2", "s2", attempt=2):
            assert current_scope() == {"plan": "p2", "story_key": "s2", "attempt": 2}
            record_usage(role="dispatch", backend="claude", model="m", input_tokens=5)

        # Exiting the inner block must RESET to the outer binding, not unbind.
        assert current_scope() == {"plan": "p", "story_key": "s", "attempt": 1}
        record_usage(role="dispatch", backend="claude", model="m", input_tokens=7)

    assert current_scope() is None
    assert is_recording() is False

    inner = _lines(plan_dir / "p2.usage.jsonl")
    assert len(inner) == 1
    assert inner[0]["attempt"] == 2
    assert inner[0]["input_tokens"] == 5

    outer = _lines(plan_dir / "p.usage.jsonl")
    assert len(outer) == 1
    assert outer[0]["attempt"] == 1
    assert outer[0]["input_tokens"] == 7


def test_current_scope_returns_a_copy(plan_dir: Path) -> None:
    with usage_scope("p", "s", attempt=1):
        snapshot = current_scope()
        assert snapshot is not None
        snapshot["plan"] = "HACK"

        record_usage(role="dispatch", backend="claude", model="m", input_tokens=1)
        assert current_scope()["plan"] == "p"

    assert _lines(plan_dir / "p.usage.jsonl")[0]["plan"] == "p"


def test_record_has_no_key_outside_usage_fields(plan_dir: Path) -> None:
    with usage_scope("plan-e", "story-5", attempt=1):
        record_usage(
            role="dispatch",
            backend="claude",
            model="m",
            input_tokens=1,
        )

    record = _lines(plan_dir / "plan-e.usage.jsonl")[0]
    assert set(record.keys()) == set(USAGE_FIELDS)
    assert "prompt" not in record
    assert "content" not in record


def test_scoped_story_call_binds_during_and_unbinds_after(plan_dir: Path) -> None:
    class Service:
        @scoped_story_call
        def dispatch_story(self, plan_name: str, story_key: str, *, extra: str = "") -> dict:
            """Dispatch one story."""
            return {"scope": current_scope(), "extra": extra, "plan": plan_name}

    service = Service()
    result = service.dispatch_story("plan-f", "story-6", extra="x")

    assert result["scope"] == {"plan": "plan-f", "story_key": "story-6", "attempt": None}
    assert result["extra"] == "x"
    assert result["plan"] == "plan-f"
    assert current_scope() is None
    assert Service.dispatch_story.__name__ == "dispatch_story"
    assert Service.dispatch_story.__doc__ == "Dispatch one story."


def test_scoped_story_call_unbinds_when_the_call_raises() -> None:
    class Service:
        @scoped_story_call
        def review_story(self, plan_name: str, story_key: str) -> None:
            assert current_scope() is not None
            raise ValueError("boom")

    with pytest.raises(ValueError):
        Service().review_story("plan-g", "story-7")

    assert current_scope() is None
    assert is_recording() is False


def test_usage_fields_are_the_documented_fixed_set() -> None:
    assert usage_record.USAGE_FIELDS == USAGE_FIELDS
    assert "plan" in USAGE_FIELDS
    assert "correlation_id" in USAGE_FIELDS
    assert "cost_usd" in USAGE_FIELDS
