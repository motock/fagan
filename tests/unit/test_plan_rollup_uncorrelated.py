"""``compute_plan_rollup`` must not count the synthetic ``"<uncorrelated>"`` bucket.

``compute_story_metrics`` publishes a ``"<uncorrelated>"`` group for records that
carry neither a ``story_key`` nor a ``correlation_id``.  That group is not a
story, so the plan rollup must drop it before it computes any total - while
``first_pass_clean_rate`` (which already filtered it) keeps its exact value.
"""

import inspect
from pathlib import Path

from pipeline import story_metrics
from pipeline.story_metrics import compute_plan_rollup, compute_story_metrics


def _story(story_key, **overrides):
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


def _uncorrelated(**overrides):
    return _story(None, **overrides)


def test_rollup_drops_uncorrelated_payload_from_totals():
    stories = [
        _story("S-1", merged=True, cost=1, first_pass_clean=True),
        _story("S-2", merged=True, cost=2, first_pass_clean=True),
        _uncorrelated(merged=True, cost=5, first_pass_clean=True),
    ]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 2
    assert rollup["stories_merged"] == 2
    assert rollup["total_cost"] == 3
    assert rollup["cost_per_merged_story"] == 1.5


def test_rollup_counts_correlation_id_only_payload_as_a_story():
    stories = [
        _story(None, correlation_id="C-1", merged=True, cost=4, first_pass_clean=True),
        _uncorrelated(merged=True, cost=9),
    ]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 1
    assert rollup["total_cost"] == 4


def test_rollup_through_producer_reports_one_story_for_two_records():
    records = [
        {"ts": "2026-09-23T00:00:00Z", "story_key": "S-1", "event": "story_merged"},
        {"ts": "2026-09-23T00:01:00Z", "story_key": "S-1", "event": "tests_failed"},
        {"ts": "2026-09-23T00:02:00Z", "event": "story_merged"},
    ]

    metrics = compute_story_metrics(records)
    # The producer still publishes the uncorrelated bucket deliberately.
    assert "<uncorrelated>" in metrics

    rollup = compute_plan_rollup(list(metrics.values()))

    assert rollup["stories_total"] == 1
    assert rollup["stories_merged"] == 1
    assert rollup["total_cost"] == 2
    assert rollup["cost_per_merged_story"] == 2.0


def test_rollup_real_story_rework_still_counts():
    stories = [
        _story("S-1", merged=True, rework_cycles=1, cost=2, first_pass_clean=True),
        _uncorrelated(merged=True, rework_cycles=3, cost=4),
    ]

    rollup = compute_plan_rollup(stories)

    assert rollup["total_rework_cycles"] == 1
    assert rollup["total_cost"] == 2
    assert rollup["cost_per_merged_story"] == 2.0


def test_rollup_empty_list_is_all_zeros_with_none_ratios():
    rollup = compute_plan_rollup([])

    assert rollup["stories_total"] == 0
    assert rollup["stories_merged"] == 0
    assert rollup["total_cost"] == 0
    assert rollup["cost_per_merged_story"] is None
    assert rollup["first_pass_clean_rate"] is None


def test_rollup_only_uncorrelated_payloads_report_zero_stories():
    stories = [_uncorrelated(merged=True, cost=3, first_pass_clean=True)]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 0
    assert rollup["total_cost"] == 0
    assert rollup["cost_per_merged_story"] is None
    assert rollup["first_pass_clean_rate"] is None


def test_rollup_payload_missing_optional_keys_is_tolerated():
    stories = [{"story_key": "S-1"}]

    rollup = compute_plan_rollup(stories)

    assert rollup["stories_total"] == 1
    assert rollup["total_cost"] == 0
    assert rollup["cost_per_merged_story"] is None


def test_rollup_first_pass_clean_rate_unchanged_by_filter():
    stories = [
        _story("S-1", merged=True, first_pass_clean=True),
        _story("S-2", merged=True, first_pass_clean=False),
        _uncorrelated(merged=True, first_pass_clean=True),
    ]

    rollup = compute_plan_rollup(stories)

    # 1 clean of 2 eligible stories; the uncorrelated payload is not eligible.
    assert rollup["first_pass_clean_rate"] == 0.5


def test_rollup_returned_key_order_is_unchanged():
    rollup = compute_plan_rollup([_story("S-1", merged=True)])

    assert list(rollup.keys()) == [
        "stories_total",
        "stories_merged",
        "total_rework_cycles",
        "total_escalations",
        "total_dispatch_failures",
        "total_cost",
        "cost_per_merged_story",
        "first_pass_clean_rate",
    ]


def test_rollup_filters_before_the_first_sum():
    source = inspect.getsource(compute_plan_rollup)

    predicate = 'story.get("story_key") is not None'
    assert predicate in source
    assert 'story.get("correlation_id") is not None' in source
    assert source.index(predicate) < source.index("stories_total = len(stories)")


def test_rework_events_are_not_first_pass_disqualifying():
    # review_changes_requested is the one deliberate exception: a reviewer bounce
    # is a dirty first pass (see the comment above the disqualifying set).
    assert (story_metrics._REWORK_EVENTS - {"review_changes_requested"}).isdisjoint(
        story_metrics._FIRST_PASS_DISQUALIFYING_EVENTS
    )


def test_disqualifying_events_definition_carries_a_rework_comment():
    lines = Path(story_metrics.__file__).read_text(encoding="utf-8").splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.startswith("_FIRST_PASS_DISQUALIFYING_EVENTS")
    )

    comment_lines = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
        comment_lines.insert(0, lines[cursor])
        cursor -= 1
    block = "\n".join(comment_lines).lower()

    assert comment_lines, "expected a comment immediately above the definition"
    assert "rework" in block
    assert "first" in block and "clean" in block
    assert "for now" not in block
    assert "rpt-1" not in block
    assert "this task" not in block


def test_plan_summary_still_counts_manifest_stories():
    source = Path("pipeline/plan_summary.py").read_text(encoding="utf-8")

    assert "from pipeline.story_metrics import compute_plan_rollup" in source
    assert "Stories: {len(stories)}" in source
