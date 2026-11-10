"""Service-level gauges derived from the audit stream (liveness S3).

Two layers are pinned here:

* :func:`pipeline.service_levels.read_story_timeline` - the three marks the
  audit stream actually records for a story: dispatch, the start of the first
  review, and the merge. The stream has no "story became eligible" event, so
  queue time is NOT measured anywhere in this file.
* :func:`pipeline.service_levels.service_level_percentiles` - fleet-wide
  nearest-rank percentiles over stories whose MERGE falls inside the window,
  and :func:`pipeline.metrics_exposition.collect` rendering them as operator
  -only gauges whose labels come from closed sets only.

Valid records are always written with the real :func:`pipeline.audit.append_audit`
against a tmp plan directory (the clock is frozen by patching the module's
``datetime`` so the record's own ``ts`` is deterministic); only deliberately
malformed lines are appended raw.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import audit as audit_mod
from pipeline import metrics_exposition as metrics
from pipeline import persistence, service_levels
from pipeline.audit_file import FileAuditLog

#: A story's three marks, as the audit stream records them.
DISPATCHED = "2025-01-01T00:00:00Z"
REVIEW_STARTED = "2025-01-01T01:00:00Z"
MERGED = "2025-01-01T03:00:00Z"


# ---------------------------------------------------------------------------
# Helpers: everything that writes an audit file or builds a snapshot lives
# here so the test bodies stay one assertion each.
# ---------------------------------------------------------------------------


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit stream at a temp plan directory for one test."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _frozen_clock(ts: str) -> type[datetime]:
    """A ``datetime`` whose ``now()`` is the moment *ts* names."""
    moment = datetime.fromisoformat(ts)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment

    return _Clock


def _append(
    monkeypatch: pytest.MonkeyPatch,
    plan_name: str,
    ts: str,
    *,
    action: str,
    outcome: str = "allowed",
    story: str = "S1",
    detail: dict | None = None,
) -> dict:
    """Append one real, hash-linked record whose ``ts`` is exactly *ts*."""
    monkeypatch.setattr(audit_mod, "datetime", _frozen_clock(ts))
    return audit_mod.append_audit(
        plan_name,
        action=action,
        outcome=outcome,
        target={"plan": plan_name, "story": story},
        detail=dict(detail or {}),
    )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    ts: str,
    plan_name: str = "p",
    story: str = "S1",
    outcome: str = "allowed",
) -> dict:
    return _append(
        monkeypatch,
        plan_name,
        ts,
        action="story.dispatch",
        outcome=outcome,
        story=story,
    )


def _review_start(
    monkeypatch: pytest.MonkeyPatch,
    started_at: str,
    plan_name: str = "p",
    story: str = "S1",
) -> dict:
    """Append a ``story.review_start`` record stamped when the call started."""
    return _append(
        monkeypatch,
        plan_name,
        started_at,
        action="story.review_start",
        story=story,
        detail={"started_at": started_at},
    )


def _merge(
    monkeypatch: pytest.MonkeyPatch,
    ts: str,
    plan_name: str = "p",
    story: str = "S1",
    outcome: str = "allowed",
) -> dict:
    return _append(
        monkeypatch,
        plan_name,
        ts,
        action="story.merge",
        outcome=outcome,
        story=story,
    )


def _trio(
    monkeypatch: pytest.MonkeyPatch, plan_name: str = "p", story: str = "S1"
) -> None:
    """Write the dispatch/review_start/merge trio for one story."""
    _dispatch(monkeypatch, DISPATCHED, plan_name=plan_name, story=story)
    _review_start(monkeypatch, REVIEW_STARTED, plan_name=plan_name, story=story)
    _merge(monkeypatch, MERGED, plan_name=plan_name, story=story)


def _append_raw(plan_dir: Path, line: str, plan_name: str = "p") -> None:
    """Append a line the audit module could never produce (malformed cases only)."""
    with (plan_dir / f"{plan_name}.audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def _log() -> FileAuditLog:
    """The audit log the service levels read through.

    The file log resolves the plan directory at call time, so the ``plan_dir``
    fixture that redirects ``pipeline.persistence.PLAN_DIR`` redirects this
    log with it.
    """
    return FileAuditLog()


def _record(action: str, ts: str, plan_name: str = "p", story: str = "S1") -> str:
    """A raw record line, for timestamps ``append_audit`` cannot be given."""
    return json.dumps(
        {
            "action": action,
            "outcome": "allowed",
            "ts": ts,
            "target": {"plan": plan_name, "story": story},
            "detail": {},
        }
    )


def _shift(ts: str, seconds: float) -> str:
    return (datetime.fromisoformat(ts) + timedelta(seconds=seconds)).isoformat()


def _cycle_stories(
    monkeypatch: pytest.MonkeyPatch, durations: list[float], merged_at: str = MERGED
) -> None:
    """One story per duration: dispatched at MERGED - duration, merged at MERGED."""
    for seconds in durations:
        story = f"S{seconds:g}"
        _dispatch(monkeypatch, _shift(merged_at, -seconds), story=story)
        _merge(monkeypatch, merged_at, story=story)


# ---------------------------------------------------------------------------
# read_story_timeline
# ---------------------------------------------------------------------------


def test_a_timeline_records_the_three_marks(monkeypatch, plan_dir) -> None:
    _trio(monkeypatch)
    timeline = service_levels.read_story_timeline(_log())
    assert timeline == {
        ("p", "S1"): {
            "dispatched": datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
            "review_started": datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
            "merged": datetime(2025, 1, 1, 3, 0, tzinfo=UTC),
        }
    }


def test_two_dispatches_use_the_first_record(monkeypatch, plan_dir) -> None:
    _dispatch(monkeypatch, "2025-01-01T00:10:00Z")
    _dispatch(monkeypatch, "2025-01-01T00:00:00Z")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, 0, 10, tzinfo=UTC)


def test_a_refused_dispatch_is_not_the_dispatch(monkeypatch, plan_dir) -> None:
    _dispatch(monkeypatch, "2025-01-01T00:10:00Z", outcome="refused")
    _dispatch(monkeypatch, "2025-01-01T05:00:00Z")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, 5, 0, tzinfo=UTC)


def test_two_review_starts_use_the_earliest_started_at(monkeypatch, plan_dir) -> None:
    _review_start(monkeypatch, "2025-01-01T02:00:00Z")
    _review_start(monkeypatch, "2025-01-01T01:00:00Z")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["review_started"] == datetime(
        2025, 1, 1, 1, 0, tzinfo=UTC
    )


def test_the_story_merge_alias_is_not_a_merge(monkeypatch, plan_dir) -> None:
    _append(monkeypatch, "p", MERGED, action="story:merge")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["merged"] is None


def test_a_refused_merge_is_not_a_merge(monkeypatch, plan_dir) -> None:
    _merge(monkeypatch, MERGED, outcome="refused")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["merged"] is None


def test_a_corrupt_line_is_skipped(monkeypatch, plan_dir) -> None:
    _append_raw(plan_dir, "{not json")
    _dispatch(monkeypatch, DISPATCHED)
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("bad_ts", ["not-a-timestamp", "2025-13-45T99:99:99Z"])
def test_a_bad_timestamp_is_skipped(monkeypatch, plan_dir, bad_ts: str) -> None:
    _append_raw(plan_dir, _record("story.dispatch", bad_ts))
    _dispatch(monkeypatch, "2025-01-01T00:05:00Z")
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, 0, 5, tzinfo=UTC)


def test_a_naive_timestamp_is_read_as_utc(monkeypatch, plan_dir) -> None:
    _append_raw(plan_dir, _record("story.dispatch", "2025-01-01T00:00:00"))
    timeline = service_levels.read_story_timeline(_log())
    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, tzinfo=UTC)


def test_an_empty_log_yields_no_timelines() -> None:
    assert service_levels.read_story_timeline(_log()) == {}


def test_the_host_audit_stream_is_not_read(monkeypatch, plan_dir) -> None:
    host_path = plan_dir / "host.audit.jsonl"
    monkeypatch.setattr(audit_mod, "host_audit_path", lambda: host_path)
    monkeypatch.setattr(audit_mod, "datetime", _frozen_clock(DISPATCHED))
    audit_mod.append_host_audit(
        action="story.dispatch",
        outcome="allowed",
        target={"plan": "p", "story": "HOST-SENTINEL-STORY"},
    )
    _dispatch(monkeypatch, DISPATCHED)
    assert sorted(service_levels.read_story_timeline(_log())) == [("p", "S1")]


# ---------------------------------------------------------------------------
# service_level_percentiles
# ---------------------------------------------------------------------------


def test_a_merged_story_contributes_the_three_durations(monkeypatch, plan_dir) -> None:
    _trio(monkeypatch)
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles == {
        "dispatch_to_review": {"n": 1, "p50": 3600.0, "p90": 3600.0},
        "review_to_merge": {"n": 1, "p50": 7200.0, "p90": 7200.0},
        "cycle": {"n": 1, "p50": 10800.0, "p90": 10800.0},
    }


@pytest.mark.parametrize(
    ("merged", "expected_n"),
    [
        ("2025-01-02T00:00:00Z", 1),
        ("2025-01-01T23:59:59Z", 0),
    ],
)
def test_the_window_lower_edge_is_inclusive(monkeypatch, plan_dir, merged, expected_n):
    _dispatch(monkeypatch, "2025-01-01T00:00:00Z")
    _merge(monkeypatch, merged)
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 2, 1, tzinfo=UTC), window_days=30
    )
    assert percentiles["cycle"]["n"] == expected_n


def test_a_story_without_a_review_start_contributes_cycle_only(
    monkeypatch, plan_dir
) -> None:
    _dispatch(monkeypatch, "2025-01-01T00:00:00Z")
    _merge(monkeypatch, "2025-01-01T02:00:00Z")
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles == {
        "dispatch_to_review": {"n": 0},
        "review_to_merge": {"n": 0},
        "cycle": {"n": 1, "p50": 7200.0, "p90": 7200.0},
    }


def test_a_story_that_never_merged_contributes_nothing(monkeypatch, plan_dir) -> None:
    _dispatch(monkeypatch, DISPATCHED)
    _review_start(monkeypatch, REVIEW_STARTED)
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles == {
        "dispatch_to_review": {"n": 0},
        "review_to_merge": {"n": 0},
        "cycle": {"n": 0},
    }


def test_a_negative_duration_is_skipped_not_clamped(monkeypatch, plan_dir) -> None:
    _dispatch(monkeypatch, "2025-01-01T00:00:00Z")
    _review_start(monkeypatch, "2025-01-01T03:00:00Z")
    _merge(monkeypatch, "2025-01-01T02:00:00Z")
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles == {
        "dispatch_to_review": {"n": 1, "p50": 10800.0, "p90": 10800.0},
        "review_to_merge": {"n": 0},
        "cycle": {"n": 1, "p50": 7200.0, "p90": 7200.0},
    }


def test_nearest_rank_percentiles_over_ten_durations(monkeypatch, plan_dir) -> None:
    _cycle_stories(monkeypatch, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles["cycle"] == {"n": 10, "p50": 50.0, "p90": 90.0}


def test_no_samples_carries_no_percentile_keys(plan_dir) -> None:
    percentiles = service_levels.service_level_percentiles(
        _log(), now=datetime(2025, 1, 2, tzinfo=UTC)
    )
    assert percentiles == {
        "dispatch_to_review": {"n": 0},
        "review_to_merge": {"n": 0},
        "cycle": {"n": 0},
    }


def test_an_empty_log_yields_no_samples() -> None:
    percentiles = service_levels.service_level_percentiles(_log())
    assert percentiles["cycle"] == {"n": 0}


# ---------------------------------------------------------------------------
# metrics_exposition: the four gauges
# ---------------------------------------------------------------------------

#: What the producer returns in the rendering tests: ten cycle samples and two
#: samples for each of the two bracketed measures.
WORKED = {
    "dispatch_to_review": {"n": 2, "p50": 3.0, "p90": 6.0},
    "review_to_merge": {"n": 2, "p50": 5.0, "p90": 10.0},
    "cycle": {"n": 10, "p50": 50.0, "p90": 90.0},
}


def _silence_other_producers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave only the service-level gauges (and the plan count) renderable."""
    monkeypatch.setattr(metrics, "health_extras", lambda plan_dir=None: None)
    monkeypatch.setattr(metrics, "outbox_summary", lambda plan_dir=None: None)
    monkeypatch.setattr(metrics, "consumer_status", lambda: None)


def _rendered(
    monkeypatch: pytest.MonkeyPatch,
    plan_dir: Path,
    percentiles: dict | None = None,
) -> str:
    _silence_other_producers(monkeypatch)
    monkeypatch.setattr(
        metrics,
        "service_level_percentiles",
        lambda *args, **kwargs: WORKED if percentiles is None else percentiles,
    )
    return metrics.render(metrics.collect(plan_dir))


@pytest.mark.parametrize(
    "expected_line",
    [
        "# HELP fagan_story_dispatch_to_review_seconds ",
        "# TYPE fagan_story_dispatch_to_review_seconds gauge",
        "# HELP fagan_story_review_to_merge_seconds ",
        "# TYPE fagan_story_review_to_merge_seconds gauge",
        "# HELP fagan_story_cycle_seconds ",
        "# TYPE fagan_story_cycle_seconds gauge",
        "# HELP fagan_story_service_level_samples ",
        "# TYPE fagan_story_service_level_samples gauge",
        'fagan_story_cycle_seconds{quantile="0.5"} 50.0',
        'fagan_story_cycle_seconds{quantile="0.9"} 90.0',
        'fagan_story_review_to_merge_seconds{quantile="0.5"} 5.0',
        'fagan_story_review_to_merge_seconds{quantile="0.9"} 10.0',
        'fagan_story_dispatch_to_review_seconds{quantile="0.5"} 3.0',
        'fagan_story_dispatch_to_review_seconds{quantile="0.9"} 6.0',
        'fagan_story_service_level_samples{measure="cycle"} 10',
        'fagan_story_service_level_samples{measure="review_to_merge"} 2',
        'fagan_story_service_level_samples{measure="dispatch_to_review"} 2',
    ],
)
def test_the_service_level_gauges_render(monkeypatch, plan_dir, expected_line) -> None:
    assert expected_line in _rendered(monkeypatch, plan_dir)


def test_a_measure_with_no_samples_emits_only_its_count(monkeypatch, plan_dir) -> None:
    rendered = _rendered(
        monkeypatch,
        plan_dir,
        {
            "dispatch_to_review": {"n": 0},
            "review_to_merge": {"n": 2, "p50": 5.0, "p90": 10.0},
            "cycle": {"n": 10, "p50": 50.0, "p90": 90.0},
        },
    )
    assert 'fagan_story_service_level_samples{measure="dispatch_to_review"} 0' in rendered
    assert "fagan_story_dispatch_to_review_seconds" not in rendered


@pytest.mark.parametrize(
    "unexpected_fragment",
    [
        'quantile="0.99"',
        "plan=",
        "story=",
        "actor=",
        "/tmp/",
    ],
)
def test_the_gauges_carry_no_extra_labels(
    monkeypatch, plan_dir, unexpected_fragment
) -> None:
    assert unexpected_fragment not in _rendered(monkeypatch, plan_dir)


def test_a_planted_story_key_never_reaches_the_exposition(monkeypatch, plan_dir) -> None:
    _silence_other_producers(monkeypatch)
    _dispatch(
        monkeypatch,
        DISPATCHED,
        plan_name="SENTINEL-PLAN-NAME",
        story="SENTINEL-STORY-KEY",
    )
    _merge(
        monkeypatch,
        MERGED,
        plan_name="SENTINEL-PLAN-NAME",
        story="SENTINEL-STORY-KEY",
    )
    monkeypatch.setattr(
        service_levels, "datetime", _frozen_clock("2025-01-02T00:00:00Z")
    )
    rendered = metrics.render(metrics.collect(plan_dir))
    assert "SENTINEL" not in rendered
    assert 'fagan_story_service_level_samples{measure="cycle"} 1' in rendered


def test_a_raising_producer_is_counted_and_omits_the_gauges(monkeypatch, plan_dir):
    def _boom(*args, **kwargs):
        raise RuntimeError("audit stream unreadable")

    _silence_other_producers(monkeypatch)
    monkeypatch.setattr(metrics, "service_level_percentiles", _boom)
    rendered = metrics.render(metrics.collect(plan_dir))
    assert 'fagan_metrics_errors_total{source="service_levels"} 1' in rendered
    assert "fagan_story_cycle_seconds" not in rendered
    assert "fagan_story_service_level_samples" not in rendered
    assert "fagan_plans" in rendered
