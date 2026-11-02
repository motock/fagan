"""Every notification event emitted by the pipeline gets a readable subject.

Guards against a regression to the legacy ``plan complete`` subject for
events that are not plan completion: every ``event="..."`` literal in
``pipeline/`` must render a humanized subject, never the legacy fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.notification_render import render_notification

PIPELINE_DIR = Path(__file__).resolve().parents[2] / "pipeline"
_EVENT_LITERAL = re.compile(r'event="([a-z_]+)"')
_LEGACY = "[pipeline] plan complete: P"


def _emitted_events() -> set[str]:
    events: set[str] = set()
    for path in PIPELINE_DIR.glob("*.py"):
        events.update(_EVENT_LITERAL.findall(path.read_text(encoding="utf-8")))
    return events


def test_the_scan_finds_pipeline_events() -> None:
    assert _emitted_events(), "scanner found no event= literals; the guard below would pass vacuously"


def test_no_emitted_event_renders_the_legacy_plan_complete_subject() -> None:
    # plan_completed is the one event that legitimately renders that subject.
    legacy = [
        event
        for event in _emitted_events()
        if event != "plan_completed"
        and render_notification(
            {"plan": "P", "payload": {"event": event, "message": "x"}}
        ).subject
        == _LEGACY
    ]
    assert legacy == []


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("usage_paused", "[pipeline] usage paused: P"),
        ("usage_resumed", "[pipeline] usage resumed: P"),
        ("story_stalled", "[pipeline] story stalled: P/S-1"),
        ("merge_conflict_rework", "[pipeline] merge rework: P/S-1"),
        ("totally_unknown_event", "[pipeline] totally unknown event: P/S-1"),
    ],
)
def test_routed_events_render_their_labels(event: str, expected: str) -> None:
    record = {"plan": "P", "story_key": "S-1", "payload": {"event": event, "message": "x"}}
    assert render_notification(record).subject == expected
