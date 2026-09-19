"""First-pass-clean signal for per-story metrics and the plan rollup.

Story under test: ``pipeline/story_metrics.py`` gains a per-group
``first_pass_clean`` boolean plus the ``disqualifying_events`` count it is
derived from, and ``compute_plan_rollup`` gains ``first_pass_clean_rate``.
``app/dashboard_helpers.py::_consolidate_stories_by_key`` collapses those
payloads into one row per story, so it must carry both new keys through or the
signal is silently dropped for every story the dashboard shows.

Contract pinned here:

  * ``first_pass_clean`` is True iff the group ``merged`` and has zero records
    whose ``event`` is in {escalated, model_fallback, story_parked,
    brief_patched}.
  * ``disqualifying_events`` is the count of that group's records carrying such
    an event.  It is published because the dashboard ORs ``merged`` across two
    groups for one story and sums the raw counters, so the consumer can only
    recompute ``first_pass_clean`` from the count - recomputing from
    ``escalations`` alone would miss parks and brief patches.
  * ``first_pass_clean_rate`` is (eligible payloads that are clean) / (eligible
    payloads), rounded to 3 decimals, where eligible means ``story_key`` or
    ``correlation_id`` is not None; ``None`` when there are no eligible
    payloads.  A payload missing the ``first_pass_clean`` key counts as not
    clean.
  * ``story_parked`` and ``brief_patched`` change no existing counter:
    ``escalations``, ``rework_cycles``, ``dispatch_failures`` and ``cost`` are
    untouched by them.

Fixture discipline (see .claude/rules/testing-config-gates.md): every record
here is a plain dict built in this file.  No test reads, globs or references the
live ``~/.claude/plans`` directory, ``PLAN_DIR``, or any real plan's
notifications file, and no assertion is keyed to today's live data.
"""

import pytest

from app.dashboard_helpers import _consolidate_stories_by_key
from pipeline import story_metrics
from pipeline.story_metrics import compute_plan_rollup, compute_story_metrics

TS_A = "2026-01-01T00:00:00+00:00"
TS_B = "2026-01-02T00:00:00+00:00"

DISQUALIFYING_EVENTS = ("escalated", "model_fallback", "story_parked", "brief_patched")

_OMIT = object()  # sentinel: leave the optional key absent entirely


def rec(story_key="S1", event=None, ts=TS_A, correlation_id=_OMIT):
    """Build one notification record as a plain dict.

    ``correlation_id=_OMIT`` (the default) leaves the key absent entirely, the
    older-record shape; ``correlation_id=None`` writes an explicit null.
    """
    record = {
        "ts": ts,
        "message": "fixture message",
        "story_key": story_key,
        "event": event,
    }
    if correlation_id is not _OMIT:
        record["correlation_id"] = correlation_id
    return record


def rollup_payload(
    story_key="S1",
    correlation_id=None,
    merged=True,
    first_pass_clean=True,
    disqualifying_events=0,
    escalations=0,
    rework_cycles=0,
    dispatch_failures=0,
    cost=1,
    merged_ts=TS_A,
):
    """Build one per-story payload in the shape ``compute_story_metrics`` emits."""
    return {
        "story_key": story_key,
        "correlation_id": correlation_id,
        "dispatch_failures": dispatch_failures,
        "rework_cycles": rework_cycles,
        "escalations": escalations,
        "merged": merged,
        "merged_ts": merged_ts,
        "cost": cost,
        "disqualifying_events": disqualifying_events,
        "first_pass_clean": first_pass_clean,
    }


# --------------------------------------------------------------------------- #
# 1. The happy path: a merged group with no disqualifying event is clean.
# --------------------------------------------------------------------------- #


def test_merged_only_group_is_first_pass_clean():
    payload = compute_story_metrics([rec("S1", "story_merged")])["S1"]
    assert payload["first_pass_clean"] is True
    assert payload["disqualifying_events"] == 0


def test_first_pass_clean_is_a_bool_not_a_truthy_counter():
    payload = compute_story_metrics([rec("S1", "story_merged")])["S1"]
    assert isinstance(payload["first_pass_clean"], bool)


# --------------------------------------------------------------------------- #
# 2. Each of the four disqualifying events disqualifies a merged group.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("event", DISQUALIFYING_EVENTS)
def test_merged_group_with_disqualifying_event_is_not_clean(event):
    records = [rec("S1", "story_merged"), rec("S1", event, ts=TS_B)]
    payload = compute_story_metrics(records)["S1"]
    assert payload["merged"] is True
    assert payload["first_pass_clean"] is False
    assert payload["disqualifying_events"] == 1


@pytest.mark.parametrize("event", DISQUALIFYING_EVENTS)
def test_disqualifying_events_counts_every_occurrence(event):
    records = [
        rec("S1", "story_merged"),
        rec("S1", event, ts=TS_A),
        rec("S1", event, ts=TS_B),
    ]
    payload = compute_story_metrics(records)["S1"]
    assert payload["disqualifying_events"] == 2
    assert payload["first_pass_clean"] is False


# --------------------------------------------------------------------------- #
# 3. Not merged is never first-pass-clean, even with no disqualifying event.
# --------------------------------------------------------------------------- #


def test_unmerged_group_is_not_first_pass_clean():
    payload = compute_story_metrics([rec("S1", "dispatch_failed")])["S1"]
    assert payload["merged"] is False
    assert payload["disqualifying_events"] == 0
    assert payload["first_pass_clean"] is False


def test_group_with_no_recognizable_event_is_not_first_pass_clean():
    payload = compute_story_metrics([rec("S1", None)])["S1"]
    assert payload["first_pass_clean"] is False
    assert payload["disqualifying_events"] == 0


# --------------------------------------------------------------------------- #
# 4. Rework events are not disqualifying.
# --------------------------------------------------------------------------- #


def test_rework_event_does_not_disqualify_a_merged_group():
    records = [rec("S1", "story_merged"), rec("S1", "tests_failed", ts=TS_B)]
    payload = compute_story_metrics(records)["S1"]
    assert payload["first_pass_clean"] is True
    assert payload["disqualifying_events"] == 0
    assert payload["rework_cycles"] == 1


# --------------------------------------------------------------------------- #
# 5. story_parked / brief_patched change no existing counter.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("event", ["story_parked", "brief_patched"])
def test_park_and_brief_patch_leave_existing_counters_untouched(event):
    records = [rec("S1", "story_merged"), rec("S1", event, ts=TS_B)]
    payload = compute_story_metrics(records)["S1"]
    assert payload["escalations"] == 0
    assert payload["rework_cycles"] == 0
    assert payload["dispatch_failures"] == 0
    assert payload["cost"] == 1


def test_park_and_brief_patch_do_not_increment_escalations():
    records = [
        rec("S1", "story_merged"),
        rec("S1", "story_parked", ts=TS_A),
        rec("S1", "brief_patched", ts=TS_B),
    ]
    payload = compute_story_metrics(records)["S1"]
    assert payload["escalations"] == 0
    assert payload["cost"] == 1
    assert payload["disqualifying_events"] == 2
    assert payload["first_pass_clean"] is False


# --------------------------------------------------------------------------- #
# 6. Correlation grouping: a disqualifying record under the same
#    correlation_id disqualifies the single collapsed group.
# --------------------------------------------------------------------------- #


def test_correlation_group_with_escalation_is_not_clean():
    records = [
        rec("S1", "story_merged", correlation_id="c-1"),
        rec(None, "escalated", ts=TS_B, correlation_id="c-1"),
    ]
    payloads = compute_story_metrics(records)
    assert list(payloads) == ["c-1"]
    payload = payloads["c-1"]
    assert payload["merged"] is True
    assert payload["disqualifying_events"] == 1
    assert payload["first_pass_clean"] is False


def test_correlation_group_without_disqualifying_event_is_clean():
    records = [
        rec("S1", "story_merged", correlation_id="c-1"),
        rec(None, "tests_failed", ts=TS_B, correlation_id="c-1"),
    ]
    payload = compute_story_metrics(records)["c-1"]
    assert payload["first_pass_clean"] is True
    assert payload["disqualifying_events"] == 0


# --------------------------------------------------------------------------- #
# 7. disqualifying_events is published on every payload, including the
#    uncorrelated group.
# --------------------------------------------------------------------------- #


def test_disqualifying_events_published_for_park():
    records = [rec("S1", "story_merged"), rec("S1", "story_parked", ts=TS_B)]
    assert compute_story_metrics(records)["S1"]["disqualifying_events"] == 1


def test_disqualifying_events_published_for_rework():
    records = [rec("S1", "story_merged"), rec("S1", "tests_failed", ts=TS_B)]
    assert compute_story_metrics(records)["S1"]["disqualifying_events"] == 0


def test_uncorrelated_group_still_carries_the_new_keys():
    payload = compute_story_metrics([rec(None, "story_parked")])[
        story_metrics.UNCORRELATED_KEY
    ]
    assert payload["disqualifying_events"] == 1
    assert payload["first_pass_clean"] is False


def test_every_payload_carries_both_new_keys():
    records = [
        rec("S1", "story_merged"),
        rec("S2", "escalated"),
        rec(None, "story_parked"),
    ]
    for payload in compute_story_metrics(records).values():
        assert "disqualifying_events" in payload
        assert "first_pass_clean" in payload


# --------------------------------------------------------------------------- #
# 8. Rollup rate: clean / eligible, rounded to 3 decimals.
# --------------------------------------------------------------------------- #


def test_rollup_rate_is_half_for_one_clean_of_two_eligible():
    stories = [
        rollup_payload(story_key="S1", first_pass_clean=True),
        rollup_payload(story_key="S2", first_pass_clean=False),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 0.5


def test_rollup_rate_rounds_to_three_decimals():
    stories = [
        rollup_payload(story_key="S1", first_pass_clean=True),
        rollup_payload(story_key="S2", first_pass_clean=True),
        rollup_payload(story_key="S3", first_pass_clean=False),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 0.667


def test_rollup_rate_is_one_when_every_eligible_payload_is_clean():
    stories = [
        rollup_payload(story_key="S1", first_pass_clean=True),
        rollup_payload(story_key="S2", first_pass_clean=True),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 1.0


def test_rollup_rate_is_zero_when_no_eligible_payload_is_clean():
    stories = [
        rollup_payload(story_key="S1", merged=False, first_pass_clean=False),
        rollup_payload(story_key="S2", merged=False, first_pass_clean=False),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 0.0


def test_rollup_rate_counts_a_correlation_only_payload_as_eligible():
    stories = [
        rollup_payload(story_key=None, correlation_id="c-9", first_pass_clean=True),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 1.0


def test_rollup_rate_excludes_the_uncorrelated_group():
    stories = [
        rollup_payload(story_key="S1", first_pass_clean=True),
        rollup_payload(story_key=None, correlation_id=None, first_pass_clean=False),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 9. Rollup boundaries: no eligible payloads -> None.
# --------------------------------------------------------------------------- #


def test_rollup_of_empty_story_list_has_none_rate():
    assert compute_plan_rollup([])["first_pass_clean_rate"] is None


def test_rollup_of_only_an_uncorrelated_payload_has_none_rate():
    stories = [{"story_key": None, "correlation_id": None, "merged": False}]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] is None


# --------------------------------------------------------------------------- #
# 10. A payload missing first_pass_clean counts as not clean.
# --------------------------------------------------------------------------- #


def test_rollup_treats_a_missing_first_pass_clean_key_as_not_clean():
    stories = [{"story_key": "S1", "correlation_id": None, "merged": True}]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 0.0


def test_rollup_missing_key_does_not_poison_a_clean_sibling():
    stories = [
        {"story_key": "S1", "correlation_id": None, "merged": True},
        rollup_payload(story_key="S2", first_pass_clean=True),
    ]
    assert compute_plan_rollup(stories)["first_pass_clean_rate"] == 0.5


# --------------------------------------------------------------------------- #
# 11. The documented contract: the module constant, the payload-key list and
#     the attribution invariant.
# --------------------------------------------------------------------------- #


def test_disqualifying_event_constant_covers_the_four_events():
    constant = getattr(story_metrics, "_FIRST_PASS_DISQUALIFYING_EVENTS", None)
    assert constant is not None
    assert set(DISQUALIFYING_EVENTS) <= set(constant)


def test_story_metrics_docstring_names_the_new_keys_and_the_invariant():
    doc = compute_story_metrics.__doc__ or ""
    assert "first_pass_clean" in doc
    assert "disqualifying_events" in doc
    assert "attribution" in doc


def test_rollup_docstring_names_the_new_rate():
    doc = compute_plan_rollup.__doc__ or ""
    assert "first_pass_clean_rate" in doc


def test_module_docstring_keeps_the_three_function_public_surface():
    assert "Public surface (exactly three functions)" in (story_metrics.__doc__ or "")


# --------------------------------------------------------------------------- #
# 12. Consolidation: a disqualifying event under EITHER group key disqualifies
#     the collapsed story row.
# --------------------------------------------------------------------------- #


def test_consolidation_disqualifies_when_either_group_key_disqualifies():
    stories = [
        rollup_payload(
            story_key="S1",
            correlation_id="c-1",
            merged=True,
            first_pass_clean=True,
            disqualifying_events=0,
        ),
        rollup_payload(
            story_key="S1",
            correlation_id=None,
            merged=False,
            first_pass_clean=False,
            disqualifying_events=1,
            merged_ts=None,
        ),
    ]
    rows = _consolidate_stories_by_key(stories)
    assert len(rows) == 1
    row = rows[0]
    assert row["story_key"] == "S1"
    assert row["merged"] is True
    assert row["disqualifying_events"] == 1
    assert row["first_pass_clean"] is False


def test_consolidation_sums_disqualifying_events_across_group_keys():
    stories = [
        rollup_payload(
            story_key="S1",
            correlation_id="c-1",
            merged=True,
            first_pass_clean=False,
            disqualifying_events=1,
        ),
        rollup_payload(
            story_key="S1",
            correlation_id=None,
            merged=False,
            first_pass_clean=False,
            disqualifying_events=2,
            merged_ts=None,
        ),
    ]
    rows = _consolidate_stories_by_key(stories)
    assert len(rows) == 1
    assert rows[0]["disqualifying_events"] == 3
    assert rows[0]["first_pass_clean"] is False


def test_consolidation_stays_clean_when_neither_group_key_disqualifies():
    stories = [
        rollup_payload(
            story_key="S1",
            correlation_id="c-1",
            merged=True,
            first_pass_clean=True,
            disqualifying_events=0,
        ),
        rollup_payload(
            story_key="S1",
            correlation_id=None,
            merged=False,
            first_pass_clean=False,
            disqualifying_events=0,
            merged_ts=None,
        ),
    ]
    rows = _consolidate_stories_by_key(stories)
    assert len(rows) == 1
    assert rows[0]["merged"] is True
    assert rows[0]["disqualifying_events"] == 0
    assert rows[0]["first_pass_clean"] is True


# --------------------------------------------------------------------------- #
# 13. Consolidation boundaries: a single clean payload, a payload missing the
#     new keys, and the dropped no-story-key group.
# --------------------------------------------------------------------------- #


def test_consolidation_of_a_single_clean_merged_payload():
    rows = _consolidate_stories_by_key([rollup_payload(story_key="S1")])
    assert len(rows) == 1
    assert rows[0]["first_pass_clean"] is True
    assert rows[0]["disqualifying_events"] == 0


def test_consolidation_of_a_single_disqualified_payload():
    stories = [
        rollup_payload(
            story_key="S1",
            merged=True,
            first_pass_clean=False,
            disqualifying_events=1,
        )
    ]
    rows = _consolidate_stories_by_key(stories)
    assert len(rows) == 1
    assert rows[0]["first_pass_clean"] is False


def test_consolidation_defaults_a_missing_disqualifying_events_key_to_zero():
    stories = [
        {
            "story_key": "S1",
            "correlation_id": None,
            "merged": True,
            "merged_ts": TS_A,
            "dispatch_failures": 0,
            "rework_cycles": 0,
            "escalations": 0,
        }
    ]
    rows = _consolidate_stories_by_key(stories)
    assert len(rows) == 1
    assert rows[0]["disqualifying_events"] == 0
    assert rows[0]["first_pass_clean"] is True


def test_consolidation_drops_a_group_with_no_story_key():
    stories = [
        rollup_payload(
            story_key=None,
            correlation_id="c-1",
            merged=True,
            first_pass_clean=True,
        )
    ]
    assert _consolidate_stories_by_key(stories) == []


def test_consolidation_keeps_the_uncorrelated_group_out_of_the_rows():
    stories = [
        rollup_payload(story_key="S1", first_pass_clean=True),
        rollup_payload(
            story_key=None,
            correlation_id=None,
            merged=False,
            first_pass_clean=False,
            disqualifying_events=1,
        ),
    ]
    rows = _consolidate_stories_by_key(stories)
    assert [row["story_key"] for row in rows] == ["S1"]
    assert rows[0]["first_pass_clean"] is True
