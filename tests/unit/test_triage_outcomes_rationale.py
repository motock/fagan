"""The human resolution carries the operator's reasoning, bounded and redacted.

``record_human_resolution`` and ``record_resolution_if_ruled`` take an optional
``rationale``. It is redacted with the same secret redaction the decisions
stream applies to story evidence, then capped at 500 characters, and is omitted
when empty. ``record_resolution_if_ruled`` still records nothing without a
ruling and still answers each ruling once.
"""

from __future__ import annotations

import json

from pipeline import persistence as ppers
from pipeline import triage_outcomes as to
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    plan_dir,
)

PLAN = "rationale-plan"
KEY = "S1"
DECIDED = "2026-11-20T10:00:00+00:00"


def _seed_ruling() -> None:
    ppers._append_decision(
        PLAN,
        {
            "story_key": KEY,
            "question": "failure triage",
            "action": "park_for_human",
            "park_class": "rework",
            "decided_by": "overlord-triage",
            "decided_at": DECIDED,
        },
    )


def _resolutions() -> list[dict]:
    path = ppers._decisions_path(PLAN)
    records = json.loads(path.read_text()) if path.exists() else []
    return [r for r in records if r.get("question") == "human resolution"]


def test_rationale_is_recorded_as_given_when_short(plan_dir) -> None:  # noqa: F811
    _seed_ruling()

    record = to.record_human_resolution(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="the old brief named the wrong file",
    )

    assert record["rationale"] == "the old brief named the wrong file"


def test_long_rationale_is_truncated_to_500_characters(plan_dir) -> None:  # noqa: F811
    _seed_ruling()

    record = to.record_human_resolution(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="x" * 1200,
    )

    assert record["rationale"] == "x" * 500


def test_rationale_is_redacted_before_it_is_stored(plan_dir) -> None:  # noqa: F811
    _seed_ruling()

    record = to.record_human_resolution(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="api_key=ABCDEF123456 was in the brief",
    )

    assert "ABCDEF123456" not in record["rationale"]
    assert "api_key=" in record["rationale"]


def test_empty_rationale_is_omitted(plan_dir) -> None:  # noqa: F811
    _seed_ruling()

    record = to.record_human_resolution(
        PLAN, KEY, human_action="rebrief", new_status="parked", principal="op",
    )

    assert "rationale" not in record


def test_if_ruled_passes_the_rationale_through_once(plan_dir) -> None:  # noqa: F811
    _seed_ruling()

    first = to.record_resolution_if_ruled(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="first",
    )
    second = to.record_resolution_if_ruled(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="second",
    )

    assert first is not None and first["rationale"] == "first"
    assert second is None
    assert len(_resolutions()) == 1


def test_if_ruled_with_no_ruling_records_nothing(plan_dir) -> None:  # noqa: F811
    result = to.record_resolution_if_ruled(
        PLAN, KEY, human_action="rebrief", new_status="parked",
        principal="op", rationale="no ruling exists",
    )

    assert result is None
    assert _resolutions() == []

