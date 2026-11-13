"""Tests for scheduler freshness in ``/api/health``.

``/api/health`` answers "is this instance alive?" through ``ok`` (always
``True``: the API answered). The scheduler verdict is additive: ``healthy``
and ``degraded`` report whether the daemon's own snapshot is recent, on top of
the outbox verdict :mod:`pipeline.outbox_health` already contributes.

Contract under test:

* :func:`pipeline.health_freshness.freshness` is pure -- no I/O, no env reads,
  never raises -- and takes the parsed health payload plus an explicit clock.
* :func:`pipeline.health_freshness.stale_seconds_from_env` re-reads
  ``PIPELINE_HEALTH_STALE_SECONDS`` on every call and falls back to 300 for a
  missing, malformed, non-finite or non-positive value.
* A missing scheduler is legitimate (``healthy`` stays True) unless the
  deployment opts in through ``PIPELINE_HEALTH_REQUIRE_SCHEDULER=1``.
* :func:`pipeline.health_freshness.health_extras` applies the outbox verdict
  last, so a stale scheduler and a stale outbox are both reported.

The ``client`` / ``plan_dir`` names are the shared
fixtures from :mod:`tests.unit._dashboard_helpers`, imported under a private
alias so a same-named test parameter is not read as an F811 redefinition of
the module-level import.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import health_freshness, paths
from tests.unit._dashboard_helpers import client, plan_dir
from tests.unit._injected_state import host_at, store_at

STALE_ENV = "PIPELINE_HEALTH_STALE_SECONDS"
REQUIRE_ENV = "PIPELINE_HEALTH_REQUIRE_SCHEDULER"
HEALTH_FILENAME = ".scheduler_health.json"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: Shared pytest fixtures re-exported for this module (see the docstring).
__all__ = ["client", "plan_dir"]


def _at(seconds_before_now: float) -> str:
    """An ISO-8601 UTC timestamp that is *seconds_before_now* seconds old."""
    return (NOW - timedelta(seconds=seconds_before_now)).isoformat()


def _recent(seconds: float) -> str:
    """An ISO-8601 UTC timestamp *seconds* old, against the real clock.

    ``health_extras`` reads ``datetime.now(UTC)`` itself, so filesystem-level
    tests cannot anchor to the fixed :data:`NOW` the pure-function tests use.
    """
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _write_outbox(path, ages) -> None:
    """Write one spool file whose records are *ages* seconds old."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": _recent(age), "message": "hello"}) for age in ages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_health(directory, payload) -> None:
    """Write the daemon's health snapshot into *directory*."""
    encoded = payload if isinstance(payload, str) else json.dumps(payload)
    (directory / HEALTH_FILENAME).write_text(encoded, encoding="utf-8")


# --------------------------------------------------------------------------
# freshness: happy paths and timestamp selection
# --------------------------------------------------------------------------


def test_fresh_heartbeat_is_healthy():
    result = health_freshness.freshness({"heartbeat_ts": _at(5)}, NOW, 60, False)
    assert result == {"scheduler_age_s": 5, "healthy": True, "degraded": []}


def test_stale_reconcile_is_stale():
    result = health_freshness.freshness(
        {"last_reconcile_ts": _at(120)}, NOW, 60, False
    )
    assert result == {
        "scheduler_age_s": 120,
        "healthy": False,
        "degraded": ["scheduler_stale"],
    }


def test_newest_timestamp_wins_over_a_stale_reconcile():
    # A fresh heartbeat outranks an old reconcile; a garbage scan is skipped
    # rather than treated as the newest (or as an absent) timestamp.
    payload = {
        "heartbeat_ts": "2026-01-01T11:59:30+00:00",
        "last_reconcile_ts": "2026-01-01T11:55:00+00:00",
        "last_scan_ts": "garbage",
    }
    result = health_freshness.freshness(payload, NOW, 60, False)
    assert result == {"scheduler_age_s": 30, "healthy": True, "degraded": []}


def test_naive_timestamp_is_treated_as_utc():
    result = health_freshness.freshness(
        {"heartbeat_ts": "2026-01-01T11:59:30"}, NOW, 60, False
    )
    assert result == {"scheduler_age_s": 30, "healthy": True, "degraded": []}


def test_last_error_alone_is_not_a_failure():
    result = health_freshness.freshness(
        {"heartbeat_ts": _at(5), "last_error": "boom"}, NOW, 60, False
    )
    assert result == {"scheduler_age_s": 5, "healthy": True, "degraded": []}


def test_non_string_timestamps_are_ignored():
    payload = {"heartbeat_ts": None, "last_reconcile_ts": 7}
    assert health_freshness.freshness(payload, NOW, 60, True) == {
        "scheduler_age_s": None,
        "healthy": False,
        "degraded": ["scheduler_unreported"],
    }


# --------------------------------------------------------------------------
# freshness: staleness boundary (strictly greater, never >= )
# --------------------------------------------------------------------------


def test_exactly_stale_seconds_is_still_healthy():
    result = health_freshness.freshness(
        {"heartbeat_ts": "2026-01-01T11:59:00+00:00"}, NOW, 60, False
    )
    assert result == {"scheduler_age_s": 60, "healthy": True, "degraded": []}


def test_one_second_over_stale_seconds_is_stale():
    result = health_freshness.freshness(
        {"heartbeat_ts": "2026-01-01T11:58:59+00:00"}, NOW, 60, False
    )
    assert result == {
        "scheduler_age_s": 61,
        "healthy": False,
        "degraded": ["scheduler_stale"],
    }


# --------------------------------------------------------------------------
# freshness: absent / unusable payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, {}, ["not", "a", "dict"], "text"])
def test_absent_payload_is_healthy_when_scheduler_is_optional(payload):
    assert health_freshness.freshness(payload, NOW, 60, False) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }


@pytest.mark.parametrize("payload", [None, {}, ["not", "a", "dict"], "text"])
def test_absent_payload_is_unreported_when_scheduler_is_required(payload):
    assert health_freshness.freshness(payload, NOW, 60, True) == {
        "scheduler_age_s": None,
        "healthy": False,
        "degraded": ["scheduler_unreported"],
    }


def test_garbage_timestamps_count_as_an_absent_payload():
    payload = {"heartbeat_ts": "garbage", "last_reconcile_ts": "not-a-date"}
    assert health_freshness.freshness(payload, NOW, 60, False) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }
    # An unusable payload is "unreported", never "stale": we do not know that
    # the daemon is behind, only that it did not report.
    assert health_freshness.freshness(payload, NOW, 60, True)["degraded"] == [
        "scheduler_unreported"
    ]


# --------------------------------------------------------------------------
# stale_seconds_from_env
# --------------------------------------------------------------------------


def test_stale_seconds_defaults_to_300_when_unset(monkeypatch):
    monkeypatch.delenv(STALE_ENV, raising=False)
    assert health_freshness.stale_seconds_from_env() == 300.0


@pytest.mark.parametrize(
    "raw", ["", "abc", "0", "-5", "inf", "-inf", "nan", "   ", "1e999"]
)
def test_stale_seconds_falls_back_to_300_when_unusable(monkeypatch, raw):
    monkeypatch.setenv(STALE_ENV, raw)
    assert health_freshness.stale_seconds_from_env() == 300.0


def test_stale_seconds_reads_a_positive_value(monkeypatch):
    monkeypatch.setenv(STALE_ENV, "10")
    assert health_freshness.stale_seconds_from_env() == 10.0


def test_stale_seconds_is_re_read_on_every_call(monkeypatch):
    monkeypatch.setenv(STALE_ENV, "10")
    assert health_freshness.stale_seconds_from_env() == 10.0
    monkeypatch.setenv(STALE_ENV, "0")
    assert health_freshness.stale_seconds_from_env() == 300.0
    monkeypatch.delenv(STALE_ENV)
    assert health_freshness.stale_seconds_from_env() == 300.0


# --------------------------------------------------------------------------
# health_extras: filesystem probe, scheduler and outbox verdicts together
# --------------------------------------------------------------------------


def test_health_extras_without_a_scheduler_file_is_healthy(tmp_path, monkeypatch):
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    result = health_freshness.health_extras(host_at(tmp_path), store_at(tmp_path))
    assert result["scheduler_age_s"] is None
    assert result["healthy"] is True
    assert result["degraded"] == []
    assert result["outbox"] == {"files": 0, "records": 0, "oldest_age_s": None}


def test_health_extras_requires_a_scheduler_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setenv(REQUIRE_ENV, "1")
    result = health_freshness.health_extras(host_at(tmp_path), store_at(tmp_path))
    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_unreported"]


def test_health_extras_treats_malformed_scheduler_json_as_absent(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    _write_health(tmp_path, "{not json")
    result = health_freshness.health_extras(host_at(tmp_path), store_at(tmp_path))
    assert result["scheduler_age_s"] is None
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_health_extras_reports_both_scheduler_stale_and_outbox_stale(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(STALE_ENV, "60")
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    # The outbox now comes from the record store, which reads the spools
    # under the plans directory: point that binding at the seeded files.
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _write_health(tmp_path, {"heartbeat_ts": _recent(300)})
    _write_outbox(tmp_path / "alpha.outbox.jsonl", [1200])

    result = health_freshness.health_extras(host_at(tmp_path), store_at(tmp_path))

    assert result["healthy"] is False
    assert result["scheduler_age_s"] == pytest.approx(300, abs=2)
    assert "scheduler_stale" in result["degraded"]
    assert "outbox_stale" in result["degraded"]
    assert result["outbox"]["records"] == 1


def test_health_extras_keeps_both_verdicts_even_when_only_the_outbox_is_stale(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(STALE_ENV, "60")
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    _write_health(tmp_path, {"heartbeat_ts": _recent(5)})
    _write_outbox(tmp_path / "alpha.outbox.jsonl", [1200])

    result = health_freshness.health_extras(host_at(tmp_path), store_at(tmp_path))

    assert result["healthy"] is False
    assert result["degraded"] == ["outbox_stale"]


def test_health_extras_ignores_a_plan_dir_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    monkeypatch.delenv(STALE_ENV, raising=False)
    result = health_freshness.health_extras(host_at(tmp_path / "missing"), store_at(tmp_path / "missing"))
    assert result["healthy"] is True
    assert result["scheduler_age_s"] is None
    assert result["degraded"] == []


# --------------------------------------------------------------------------
# /api/health through the real app
# --------------------------------------------------------------------------


def test_api_health_reports_freshness_without_changing_ok(
    client, plan_dir, monkeypatch
):
    monkeypatch.setenv(STALE_ENV, "60")
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    # The outbox now comes from the record store, which reads the spools
    # under the plans directory: point that binding at the seeded files.
    monkeypatch.setattr(paths, "PLAN_DIR", plan_dir)
    _write_health(plan_dir, {"heartbeat_ts": _at(300)})
    _write_outbox(plan_dir / "alpha.outbox.jsonl", [1200])

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["healthy"] is False
    assert "outbox" in body
    assert "outbox_stale" in body["degraded"]
    assert "scheduler_stale" in body["degraded"]


def test_api_health_stays_ok_when_the_scheduler_is_simply_absent(
    client, plan_dir, monkeypatch
):
    monkeypatch.delenv(STALE_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_ENV, raising=False)

    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["healthy"] is True
    assert body["degraded"] == []
    assert body["scheduler_age_s"] is None
    assert "outbox" in body
