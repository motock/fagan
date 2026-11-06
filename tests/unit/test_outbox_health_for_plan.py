"""Tests for :func:`pipeline.outbox_health.outbox_summary_for_plan`.

The aggregate :func:`pipeline.outbox_health.outbox_summary` answers "is the
whole outbox stalling?". Liveness signal S7 needs the same numbers for exactly
one plan, so a team sees only its own backlog: the same
``{"files", "records", "oldest_age_s"}`` shape, but counting only the spool
files that belong to that plan.

Ownership is decided by exact name, never by prefix, so plan ``a`` can never
absorb the files of plan ``ab`` or of a plan literally named ``a.outbox``. The
per-channel form ``<plan>.outbox.<channel>.jsonl`` only matches while
``<channel>`` carries no dot -- a dotted remainder belongs to some other plan's
name, not to a channel of this one.

Every timestamp is derived from ``datetime.now(UTC)`` so ages are deterministic
without hard-coding a clock value; the summary truncates to whole seconds, so a
timestamp written at age ``N`` reads back as exactly ``N``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import outbox_health

TOLERANCE_S = 2

EMPTY_SUMMARY = {"files": 0, "records": 0, "oldest_age_s": None}


def _ts(age_s: float) -> str:
    """An ISO-8601 UTC timestamp that is *age_s* seconds old."""
    return (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()


def _record(age_s: float, message: str = "hello") -> str:
    return json.dumps({"ts": _ts(age_s), "message": message})


def _write(path, lines) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _assert_summary(summary, files: int, records: int, oldest_age_s) -> None:
    assert set(summary) == {"files", "records", "oldest_age_s"}
    assert summary["files"] == files
    assert summary["records"] == records
    if oldest_age_s is None:
        assert summary["oldest_age_s"] is None
    else:
        assert summary["oldest_age_s"] == pytest.approx(oldest_age_s, abs=TOLERANCE_S)


# --------------------------------------------------------------------------
# Counting one plan's files
# --------------------------------------------------------------------------


def test_counts_only_the_named_plan(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(10)])
    _write(tmp_path / "beta.outbox.jsonl", [_record(20), _record(30)])

    _assert_summary(
        outbox_health.outbox_summary_for_plan(tmp_path, "alpha"), 1, 1, 10
    )


@pytest.mark.parametrize(
    "spool_name",
    [
        "alpha.outbox.jsonl",
        "alpha.outbox.slack.jsonl",
        "alpha.outbox.email.jsonl",
    ],
)
def test_per_plan_and_per_channel_names_both_match(tmp_path, spool_name):
    _write(tmp_path / spool_name, [_record(60)])

    _assert_summary(
        outbox_health.outbox_summary_for_plan(tmp_path, "alpha"), 1, 1, 60
    )


def test_plan_and_channel_files_are_summed_together(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(10)])
    _write(tmp_path / "alpha.outbox.slack.jsonl", [_record(20)])

    _assert_summary(
        outbox_health.outbox_summary_for_plan(tmp_path, "alpha"), 2, 2, 20
    )


@pytest.mark.parametrize(
    "lookalike",
    [
        "ab.outbox.jsonl",
        "a.outbox.outbox.slack.jsonl",
        "a.outboxx.jsonl",
        "a.outbox..jsonl",
        "x.a.outbox.jsonl",
    ],
)
def test_plan_a_ignores_lookalike_names(tmp_path, lookalike):
    _write(tmp_path / "a.outbox.jsonl", [_record(5)])
    _write(tmp_path / lookalike, [_record(900)])

    _assert_summary(outbox_health.outbox_summary_for_plan(tmp_path, "a"), 1, 1, 5)


def test_subdirectories_are_not_scanned(tmp_path):
    _write(tmp_path / "nested" / "alpha.outbox.jsonl", [_record(10)])

    assert outbox_health.outbox_summary_for_plan(tmp_path, "alpha") == EMPTY_SUMMARY


# --------------------------------------------------------------------------
# Agreement with the aggregate summary, and age aggregation
# --------------------------------------------------------------------------


def test_sum_over_plans_equals_the_aggregate_summary(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(10), _record(20)])
    _write(tmp_path / "alpha.outbox.slack.jsonl", [_record(30)])
    _write(tmp_path / "beta.outbox.jsonl", [_record(40)])

    alpha = outbox_health.outbox_summary_for_plan(tmp_path, "alpha")
    beta = outbox_health.outbox_summary_for_plan(tmp_path, "beta")
    aggregate = outbox_health.outbox_summary(tmp_path)

    assert alpha["files"] + beta["files"] == aggregate["files"] == 3
    assert alpha["records"] + beta["records"] == aggregate["records"] == 4


def test_oldest_age_is_the_max_across_that_plans_files(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(50), _record(700)])
    _write(tmp_path / "alpha.outbox.slack.jsonl", [_record(1200)])
    _write(tmp_path / "beta.outbox.jsonl", [_record(5000)])

    _assert_summary(
        outbox_health.outbox_summary_for_plan(tmp_path, "alpha"), 2, 3, 1200
    )


def test_corrupt_line_counts_as_a_record_without_breaking_the_age(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", ["{not json at all", _record(300)])

    _assert_summary(
        outbox_health.outbox_summary_for_plan(tmp_path, "alpha"), 1, 2, 300
    )


def test_a_directory_shaped_like_a_spool_file_is_skipped(tmp_path):
    (tmp_path / "alpha.outbox.jsonl").mkdir()

    assert outbox_health.outbox_summary_for_plan(tmp_path, "alpha") == EMPTY_SUMMARY


# --------------------------------------------------------------------------
# Negative cases: never raises, always the empty summary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("plan_name", ["", "../x", "a/b", None, 42])
def test_invalid_plan_name_returns_the_empty_summary(tmp_path, plan_name):
    _write(tmp_path / "a.outbox.jsonl", [_record(10)])

    assert outbox_health.outbox_summary_for_plan(tmp_path, plan_name) == EMPTY_SUMMARY


def test_missing_directory_returns_the_empty_summary(tmp_path):
    assert (
        outbox_health.outbox_summary_for_plan(tmp_path / "missing", "alpha")
        == EMPTY_SUMMARY
    )
