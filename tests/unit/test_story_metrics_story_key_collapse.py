"""``compute_plan_rollup`` must collapse the split groups of one story.

``compute_story_metrics`` groups by ``correlation_id`` FIRST and only falls back
to ``story_key``, so one story whose records carry both shapes yields two
payloads: ``stories_total`` is inflated, ``cost_per_merged_story`` diluted, and
``first_pass_clean`` split.  The rollup must fold them back into one story -
summing counters and RECOMPUTING ``cost`` from them (each payload's cost already
includes its own base 1, so summing costs double-counts the story).
pipeline/wedge_io.py's wedge notice triggers this by passing ``story_key`` but
never ``correlation_id``; the wedge tests pin that it now carries one.
"""

from __future__ import annotations

import copy
import inspect

import pytest

import pipeline.server as p
from pipeline import config, story_metrics, wedge_io
from pipeline.story_metrics import compute_plan_rollup, compute_story_metrics

PLAN = "wp"
KEY = "s1"


def _payload(story_key, **overrides):
    payload = {
        "story_key": story_key,
        "correlation_id": None,
        "dispatch_failures": 0,
        "rework_cycles": 0,
        "escalations": 0,
        "merged": False,
        "merged_ts": None,
        "cost": 1,
        "disqualifying_events": 0,
        "first_pass_clean": False,
    }
    payload.update(overrides)
    return payload


def test_split_groups_for_one_story_count_once():
    records = [
        {"ts": "t0", "story_key": "S-1", "correlation_id": "C-1", "event": "tests_failed"},
        {"ts": "t1", "story_key": "S-1", "correlation_id": "C-1", "event": "story_merged"},
        {"ts": "t2", "story_key": "S-1", "event": "wedge"},
    ]

    metrics = compute_story_metrics(records)
    # The producer still publishes one payload per group: C-1 and S-1.
    assert len(metrics) == 2

    rollup = compute_plan_rollup(list(metrics.values()))

    assert rollup["stories_total"] == 1
    assert rollup["stories_merged"] == 1
    # Recomputed from the summed counters (1 + 1 rework), NOT summed costs (3).
    assert rollup["total_cost"] == 2
    assert rollup["cost_per_merged_story"] == 2.0


def test_merged_counters_are_recomputed_not_summed():
    target = _payload("S-1", dispatch_failures=1, rework_cycles=2, cost=4)
    source = _payload("S-1", rework_cycles=1, escalations=3, cost=5)

    story_metrics._merge_payload(target, source)

    assert target["dispatch_failures"] == 1
    assert target["rework_cycles"] == 3
    assert target["escalations"] == 3
    assert target["cost"] == 1 + 1 + 3 + 3

    rollup = compute_plan_rollup(
        [
            _payload("S-1", dispatch_failures=1, rework_cycles=2, cost=4),
            _payload("S-1", rework_cycles=1, escalations=3, cost=5),
        ]
    )
    assert rollup["stories_total"] == 1
    assert rollup["total_dispatch_failures"] == 1
    assert rollup["total_rework_cycles"] == 3
    assert rollup["total_escalations"] == 3
    assert rollup["total_cost"] == 1 + 1 + 3 + 3


def test_disqualifying_event_from_either_group_disqualifies_the_story():
    stories = [
        _payload("S-1", merged=True, first_pass_clean=True),
        _payload("S-1", disqualifying_events=1),
    ]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 1
    # Merged but carrying a disqualifying event: 0 clean of 1 eligible.
    assert rollup["first_pass_clean_rate"] == 0.0


def test_keyless_payloads_are_never_collapsed():
    stories = [
        _payload(None, correlation_id="C-1", merged=True, cost=4),
        _payload(None, merged=True, cost=9),  # the "<uncorrelated>" bucket
    ]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 1
    assert rollup["total_cost"] == 4

    # Two correlation_id-only payloads are two stories, not one.
    two = compute_plan_rollup(
        [
            _payload(None, correlation_id="C-1", merged=True, cost=4),
            _payload(None, correlation_id="C-2", merged=True, cost=5),
        ]
    )
    assert two["stories_total"] == 2
    assert two["total_cost"] == 9

    # The producer still publishes the synthetic bucket for keyless records.
    metrics = compute_story_metrics([{"ts": "t0", "event": "wedge"}])
    assert "<uncorrelated>" in metrics


def test_input_payloads_are_not_mutated():
    stories = [
        _payload("S-1", merged=True, cost=1, first_pass_clean=True),
        _payload("S-1", rework_cycles=1, cost=2),
    ]
    before = copy.deepcopy(stories)

    compute_plan_rollup(stories)

    assert stories == before


def test_single_payload_for_a_story_is_unchanged():
    stories = [_payload("S-1", merged=True, cost=3, first_pass_clean=True)]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 1
    assert rollup["stories_merged"] == 1
    assert rollup["total_cost"] == 3


def test_collapse_helpers_exist():
    assert callable(story_metrics._merge_payload)
    assert callable(story_metrics._collapse_shared_story_keys)


def test_collapse_runs_before_the_filter():
    source = inspect.getsource(compute_plan_rollup)

    collapse = "_collapse_shared_story_keys(stories)"
    assert collapse in source
    assert source.index(collapse) < source.index('story.get("story_key") is not None')


class _StubStore:
    def __init__(self, manifest):
        self.manifest = manifest

    def get_manifest_or_none(self, plan_name):
        return self.manifest


@pytest.fixture(autouse=True)
def _clear_wedge_state():
    """Clear wedge_io's module-level dicts (the cooldown table) around each test."""

    def _clear():
        for key, value in vars(wedge_io).items():
            if not key.startswith("__") and isinstance(value, dict):
                value.clear()

    _clear()
    yield
    _clear()


def _wedge_rig(monkeypatch, story):
    monkeypatch.setattr(config, "WEDGE_SCAN_ENABLED", 1)
    monkeypatch.setattr(config, "WEDGE_STALE_ACTIVITY_SECONDS", 1800)
    monkeypatch.setattr(config, "WEDGE_NOTIFY_COOLDOWN_SECONDS", 600)
    monkeypatch.setattr(p, "_store", _StubStore({"stories": {KEY: story}}))
    emitted = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: emitted.append((a, k)))
    monkeypatch.setattr(wedge_io, "_pid_is_alive", lambda pid: False)
    return emitted


def test_wedge_notice_carries_the_story_correlation_id(plan_dir, monkeypatch):
    emitted = _wedge_rig(
        monkeypatch, {"status": "in_progress", "pid": 12345, "correlation_id": "C-1"}
    )

    wedge_io.run_wedge_scan(PLAN)

    assert emitted, "expected the dead-pid wedge notice to be emitted"
    for _, kwargs in emitted:
        assert kwargs["correlation_id"] == "C-1"


def test_wedge_notice_omits_correlation_id_when_absent(plan_dir, monkeypatch):
    emitted = _wedge_rig(monkeypatch, {"status": "in_progress", "pid": 12345})

    wedge_io.run_wedge_scan(PLAN)

    assert emitted, "expected the dead-pid wedge notice to be emitted"
    for _, kwargs in emitted:
        assert "correlation_id" not in kwargs
