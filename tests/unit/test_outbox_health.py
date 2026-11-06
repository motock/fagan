"""Tests for :mod:`pipeline.outbox_health`.

The summary is a read-only probe of the notification outbox: how many files,
how many records, and how old the oldest record is. ``/api/health`` turns that
into the ``outbox_stale`` verdict, and the tick checks turn the transitions
into exactly one notice per stall (and one on recovery).

Every timestamp is derived from ``datetime.now(UTC)`` so ages are
deterministic without hard-coding a clock value; the summary truncates to whole
seconds, so a timestamp written at age ``N`` reads back as exactly ``N``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import outbox_health

TOLERANCE_S = 2


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
# outbox_summary
# --------------------------------------------------------------------------


def test_empty_dir_is_all_zero(tmp_path):
    assert outbox_health.outbox_summary(tmp_path) == {
        "files": 0,
        "records": 0,
        "oldest_age_s": None,
    }


def test_one_fresh_record(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(100)])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 1, 100)


def test_one_stale_record(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(1200)])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 1, 1200)


def test_oldest_age_is_the_oldest_valid_record(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(50), _record(1200), _record(900)])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 3, 1200)


def test_bad_lines_are_records_with_unknown_age(tmp_path):
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [_record(1200), "not json", json.dumps({"ts": "garbage"})],
    )

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 3, 1200)


def test_file_of_only_bad_lines_has_no_age(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", ["not json"])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 1, None)


def test_missing_dir_is_all_zero_and_does_not_raise(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert outbox_health.outbox_summary(missing) == {
        "files": 0,
        "records": 0,
        "oldest_age_s": None,
    }


def test_per_channel_outbox_counts_and_sums(tmp_path):
    # ``<plan>.outbox.jsonl`` plus a per-channel ``<plan>.outbox.<channel>.jsonl``
    # (the shape ``notification_email`` spools).
    _write(tmp_path / "alpha.outbox.jsonl", [_record(10), _record(20)])
    _write(tmp_path / "alpha.outbox.email.jsonl", [_record(30)])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 2, 3, 30)


def test_nested_outbox_file_is_counted(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(10)])
    _write(tmp_path / "nested" / "beta.outbox.jsonl", [_record(40)])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 2, 2, 40)


def test_channel_file_with_bad_line_keeps_the_valid_age(tmp_path):
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [_record(50), _record(1200), _record(900)],
    )
    _write(tmp_path / "chan-b.outbox.jsonl", [_record(5), "not json"])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 2, 5, 1200)


def test_non_json_scalar_line_is_unknown_age(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", ["42", json.dumps({"ts": 7})])

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 2, None)


def test_naive_timestamp_is_treated_as_utc(tmp_path):
    naive = (datetime.now(UTC) - timedelta(seconds=1200)).replace(tzinfo=None)
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [json.dumps({"ts": naive.isoformat()})],
    )

    _assert_summary(outbox_health.outbox_summary(tmp_path), 1, 1, 1200)


def test_payload_text_never_reaches_the_summary(tmp_path):
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [_record(1200, message="SECRET-PAYLOAD-XYZ")],
    )

    summary = outbox_health.outbox_summary(tmp_path)

    assert "SECRET-PAYLOAD-XYZ" not in json.dumps(summary)
    assert set(summary) == {"files", "records", "oldest_age_s"}


def test_summary_does_not_create_state(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()

    outbox_health.outbox_summary(plan_dir)

    assert list(plan_dir.iterdir()) == []


# --------------------------------------------------------------------------
# threshold: env parsing + the strict boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", "inf", "nan", "  "])
def test_malformed_or_non_positive_threshold_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, raw)

    assert outbox_health.outbox_stale_seconds() == 900


def test_valid_threshold_is_used(monkeypatch):
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "60")

    assert outbox_health.outbox_stale_seconds() == 60


def test_threshold_is_reread_on_every_call(monkeypatch):
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "60")
    assert outbox_health.outbox_stale_seconds() == 60

    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "120")
    assert outbox_health.outbox_stale_seconds() == 120


def test_boundary_exactly_at_threshold_is_not_stale(monkeypatch, tmp_path):
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "900")
    _write(tmp_path / "alpha.outbox.jsonl", [_record(900)])

    summary = outbox_health.outbox_summary(tmp_path)

    assert summary["oldest_age_s"] == 900
    assert outbox_health.outbox_is_stale(summary, outbox_health.outbox_stale_seconds()) is False


def test_boundary_one_second_past_threshold_is_stale(monkeypatch, tmp_path):
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "900")
    _write(tmp_path / "alpha.outbox.jsonl", [_record(901)])

    summary = outbox_health.outbox_summary(tmp_path)

    assert summary["oldest_age_s"] == 901
    assert outbox_health.outbox_is_stale(summary, outbox_health.outbox_stale_seconds()) is True


def test_unknown_age_is_never_stale():
    summary = {"files": 1, "records": 1, "oldest_age_s": None}

    assert outbox_health.outbox_is_stale(summary, 900) is False


# --------------------------------------------------------------------------
# apply_outbox_health: the /api/health contribution
# --------------------------------------------------------------------------


def test_health_block_marks_a_stale_outbox(tmp_path):
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [_record(50), _record(1200), _record(900)],
    )
    _write(tmp_path / "chan-b.outbox.jsonl", [_record(5), "not json"])
    result = {"healthy": True, "degraded": ["config_mismatch"]}

    out = outbox_health.apply_outbox_health(result, tmp_path, threshold=900)

    assert out is result
    _assert_summary(out["outbox"], 2, 5, 1200)
    assert out["healthy"] is False
    assert out["degraded"] == ["config_mismatch", "outbox_stale"]


def test_health_block_leaves_a_threshold_age_healthy(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(900)])
    result = {"healthy": True, "degraded": []}

    out = outbox_health.apply_outbox_health(result, tmp_path, threshold=900)

    _assert_summary(out["outbox"], 1, 1, 900)
    assert out["healthy"] is True
    assert out["degraded"] == []


def test_health_block_with_unknown_age_leaves_health_alone(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", ["not json"])
    result = {"healthy": True, "degraded": []}

    out = outbox_health.apply_outbox_health(result, tmp_path, threshold=900)

    _assert_summary(out["outbox"], 1, 1, None)
    assert out["healthy"] is True
    assert out["degraded"] == []


def test_health_block_does_not_duplicate_the_verdict(tmp_path):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(1200)])
    result = {"healthy": True, "degraded": ["outbox_stale"]}

    out = outbox_health.apply_outbox_health(result, tmp_path, threshold=900)

    assert out["degraded"] == ["outbox_stale"]


def test_health_block_reads_the_threshold_from_the_env(tmp_path, monkeypatch):
    _write(tmp_path / "alpha.outbox.jsonl", [_record(120)])
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "60")
    result = {"healthy": True, "degraded": []}

    out = outbox_health.apply_outbox_health(result, tmp_path)

    assert out["healthy"] is False
    assert out["degraded"] == ["outbox_stale"]


def test_health_block_carries_no_names_paths_or_payloads(tmp_path):
    _write(
        tmp_path / "alpha.outbox.jsonl",
        [_record(1200, message="SECRET-PAYLOAD-XYZ")],
    )
    result = {"healthy": True, "degraded": []}

    out = outbox_health.apply_outbox_health(result, tmp_path, threshold=900)
    dumped = json.dumps(out)

    assert "SECRET-PAYLOAD-XYZ" not in dumped
    assert "alpha" not in dumped
    assert str(tmp_path) not in dumped
    assert set(out["outbox"]) == {"files", "records", "oldest_age_s"}


# --------------------------------------------------------------------------
# _stale_transition: exactly one notice per transition
# --------------------------------------------------------------------------


def test_stale_then_still_stale_then_recovered_then_quiet():
    prev = False

    event, prev = outbox_health._stale_transition(prev, 1200, 900)
    assert (event, prev) == ("outbox_stale", True)

    event, prev = outbox_health._stale_transition(prev, 1500, 900)
    assert (event, prev) == (None, True)

    event, prev = outbox_health._stale_transition(prev, 10, 900)
    assert (event, prev) == ("outbox_recovered", False)

    event, prev = outbox_health._stale_transition(prev, 200, 900)
    assert (event, prev) == (None, False)


def test_boundary_transition_leaves_the_correct_state_behind():
    prev = False

    event, prev = outbox_health._stale_transition(prev, 1200, 900)
    assert (event, prev) == ("outbox_stale", True)

    event, prev = outbox_health._stale_transition(prev, 900, 900)
    assert (event, prev) == ("outbox_recovered", False)

    event, prev = outbox_health._stale_transition(prev, 900, 900)
    assert (event, prev) == (None, False)


def test_unknown_age_recovers_a_stalled_outbox():
    prev = False

    event, prev = outbox_health._stale_transition(prev, 1200, 900)
    assert (event, prev) == ("outbox_stale", True)

    event, prev = outbox_health._stale_transition(prev, None, 900)
    assert (event, prev) == ("outbox_recovered", False)

    event, prev = outbox_health._stale_transition(prev, 1000, 900)
    assert (event, prev) == ("outbox_stale", True)


def test_transition_returns_the_stored_flag_last():
    # Guards the classic bug: the right event with the wrong persisted flag
    # re-fires the notice on the next tick, or suppresses the next real stall.
    for prev_stale in (False, True):
        for age in (None, 0, 900, 901):
            event, new_stale = outbox_health._stale_transition(prev_stale, age, 900)
            stale = age is not None and age > 900
            assert new_stale is stale
            if not prev_stale and stale:
                assert event == "outbox_stale"
            elif prev_stale and not stale:
                assert event == "outbox_recovered"
            else:
                assert event is None


# --------------------------------------------------------------------------
# run_tick_checks wiring: one notice per stall, one on recovery
# --------------------------------------------------------------------------


class _FakeDaemon:
    def __init__(self) -> None:
        self.stale_calls = 0

    def _notify_if_checkout_stale(self, config) -> None:
        self.stale_calls += 1


@pytest.fixture()
def tick_env(tmp_path, monkeypatch):
    from pipeline import paths, persistence, tick_checks

    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)
    monkeypatch.setenv(outbox_health.OUTBOX_STALE_ENV, "900")
    monkeypatch.setattr(tick_checks, "_OUTBOX_STALE", False)
    (tmp_path / "alpha.manifest.json").write_text("{}", encoding="utf-8")

    events = []

    def fake_notify(plan, message, **kwargs):
        events.append((plan, kwargs.get("event"), message))

    monkeypatch.setattr(persistence, "_notify_user", fake_notify)
    return tmp_path, tick_checks, events


def test_run_tick_checks_notifies_once_per_stall(tick_env):
    plan_dir, tick_checks, events = tick_env
    _write(plan_dir / "alpha.outbox.jsonl", [_record(1200)])
    daemon = _FakeDaemon()

    tick_checks.run_tick_checks(daemon, {})
    tick_checks.run_tick_checks(daemon, {})
    tick_checks.run_tick_checks(daemon, {})

    assert [event for _, event, _ in events] == ["outbox_stale"]
    assert daemon.stale_calls == 3
    # Aggregate only: the notice is attributed to the first manifest plan.
    assert events[0][0] == "alpha"


def test_run_tick_checks_notifies_on_recovery(tick_env):
    plan_dir, tick_checks, events = tick_env
    _write(plan_dir / "alpha.outbox.jsonl", [_record(1200)])
    daemon = _FakeDaemon()

    tick_checks.run_tick_checks(daemon, {})

    _write(plan_dir / "alpha.outbox.jsonl", [_record(5)])
    tick_checks.run_tick_checks(daemon, {})

    assert [event for _, event, _ in events] == ["outbox_stale", "outbox_recovered"]


def test_run_tick_checks_survives_a_broken_outbox_probe(tick_env, monkeypatch):
    _, tick_checks, events = tick_env

    def boom(_plan_dir):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(outbox_health, "outbox_summary", boom)

    tick_checks.run_tick_checks(_FakeDaemon(), {})

    assert events == []
