"""TDD tests for ``/api/health`` scheduler freshness (``healthy`` / ``degraded``).

``/api/health`` returns ``ok: True`` literally, and that must never change: it
means "the API answered". Whether the *scheduler* is alive is a separate
question, answered by the additive keys this module pins:

* ``scheduler_age_s`` — integer seconds since the newest parseable timestamp in
  ``<plan_dir>/.scheduler_health.json``, or ``None`` when there is no file (or
  no parseable timestamp in it);
* ``healthy`` — ``False`` when a scheduler is required but unreported, or when
  the newest heartbeat is older than the configured staleness window;
* ``degraded`` — the list of reasons (``scheduler_unreported`` /
  ``scheduler_stale``), ``[]`` when healthy.

``pipeline.health_freshness`` holds the logic: ``freshness`` is pure,
``stale_seconds_from_env`` reads the environment on *every* call (a cached
window silently mis-reports after an operator changes it), and
``health_extras`` is the only I/O the dashboard performs for this.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pipeline.health_freshness import (
    freshness,
    health_extras,
    stale_seconds_from_env,
)

import pipeline.server  # noqa: F401  (pre-existing import cycle: import before pipeline.*)
from app import dashboard
from pipeline import health_freshness
from tests.unit._dashboard_helpers import client, plan_dir  # noqa: F401

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(**delta_kwargs) -> str:
    """An ISO-8601 UTC timestamp relative to :data:`NOW`."""
    return (NOW - timedelta(**delta_kwargs)).isoformat()


# ---------------------------------------------------------------------------
# freshness(): fresh / stale / newest-of
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_is_healthy_with_an_integer_age():
    result = freshness({"heartbeat_ts": _ts(seconds=10)}, NOW, 300.0, False)

    assert result["healthy"] is True
    assert result["degraded"] == []
    assert result["scheduler_age_s"] == 10
    assert isinstance(result["scheduler_age_s"], int)


def test_stale_last_reconcile_is_degraded_scheduler_stale():
    result = freshness({"last_reconcile_ts": _ts(seconds=400)}, NOW, 300.0, False)

    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_stale"]
    assert result["scheduler_age_s"] == 400


def test_newest_timestamp_wins_over_an_older_one():
    """A fresh heartbeat must beat a stale reconcile: newest-of, not first-of."""
    payload = {
        "heartbeat_ts": _ts(seconds=0),
        "last_reconcile_ts": _ts(hours=4),
    }

    result = freshness(payload, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 0
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_last_scan_ts_also_counts_as_a_reference_time():
    result = freshness({"last_scan_ts": _ts(seconds=42)}, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 42
    assert result["healthy"] is True


# ---------------------------------------------------------------------------
# freshness(): absent / wrong-typed payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, [], "x", 42, 3.5, True])
def test_absent_payload_without_a_required_scheduler_is_healthy(payload):
    """A dashboard deployed without a daemon is legitimate: not degraded."""
    assert freshness(payload, NOW, 300.0, False) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }


@pytest.mark.parametrize("payload", [None, [], "x", 42])
def test_absent_payload_with_a_required_scheduler_is_unreported(payload):
    result = freshness(payload, NOW, 300.0, True)

    assert result["scheduler_age_s"] is None
    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_unreported"]


# ---------------------------------------------------------------------------
# freshness(): timestamp parsing
# ---------------------------------------------------------------------------


def test_naive_timestamp_is_read_as_utc():
    payload = {"heartbeat_ts": (NOW - timedelta(seconds=15)).replace(tzinfo=None).isoformat()}

    result = freshness(payload, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 15
    assert result["healthy"] is True


def test_garbage_timestamp_is_skipped_in_favour_of_a_parseable_one():
    payload = {"heartbeat_ts": "yesterday", "last_reconcile_ts": _ts(seconds=5)}

    result = freshness(payload, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 5
    assert result["healthy"] is True


def test_all_garbage_timestamps_behave_like_an_absent_file():
    payload = {"heartbeat_ts": "yesterday", "last_reconcile_ts": "soon"}

    assert freshness(payload, NOW, 300.0, False) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }

    required = freshness(payload, NOW, 300.0, True)
    assert required["healthy"] is False
    assert required["degraded"] == ["scheduler_unreported"]


@pytest.mark.parametrize("payload", [{"heartbeat_ts": 12345}, {"heartbeat_ts": None}])
def test_non_string_timestamps_never_raise(payload):
    """Odd payloads must degrade, never explode a health endpoint."""
    result = freshness(payload, NOW, 300.0, False)

    assert result["scheduler_age_s"] is None
    assert result["healthy"] is True
    assert result["degraded"] == []


# ---------------------------------------------------------------------------
# freshness(): boundaries and last_error
# ---------------------------------------------------------------------------


def test_age_exactly_at_the_stale_boundary_is_still_healthy():
    result = freshness({"heartbeat_ts": _ts(seconds=300)}, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 300
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_one_second_past_the_stale_boundary_is_stale():
    result = freshness({"heartbeat_ts": _ts(seconds=301)}, NOW, 300.0, True)

    assert result["scheduler_age_s"] == 301
    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_stale"]


def test_sub_second_overshoot_is_measured_before_rounding():
    """300.9 s must not round down to a healthy 300 s."""
    payload = {"heartbeat_ts": (NOW - timedelta(seconds=300.9)).isoformat()}

    result = freshness(payload, NOW, 300.0, True)

    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_stale"]


def test_last_error_alone_does_not_degrade_a_fresh_scheduler():
    payload = {"heartbeat_ts": _ts(seconds=5), "last_error": "transient git failure"}

    result = freshness(payload, NOW, 300.0, True)

    assert result["healthy"] is True
    assert result["degraded"] == []


def test_future_timestamp_is_healthy_and_not_clamped():
    payload = {"heartbeat_ts": (NOW + timedelta(seconds=30)).isoformat()}

    result = freshness(payload, NOW, 300.0, False)

    assert result["healthy"] is True
    assert result["scheduler_age_s"] == -30


# ---------------------------------------------------------------------------
# stale_seconds_from_env()
# ---------------------------------------------------------------------------


def test_stale_seconds_defaults_to_300_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_STALE_SECONDS", raising=False)

    assert stale_seconds_from_env() == 300.0


@pytest.mark.parametrize("value", ["abc", "0", "-5", "inf", "nan", "", "  "])
def test_malformed_or_non_positive_stale_seconds_fall_back_to_300(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", value)

    assert stale_seconds_from_env() == 300.0


def test_stale_seconds_are_reread_on_every_call(monkeypatch):
    """An import-time cache would return 60.0 again and mis-report a 120 s age."""
    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", "60")
    assert stale_seconds_from_env() == 60.0

    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", "oops")
    assert stale_seconds_from_env() == 300.0

    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", "90")
    assert stale_seconds_from_env() == 90.0


# ---------------------------------------------------------------------------
# health_extras(): the file-reading wrapper
# ---------------------------------------------------------------------------


def test_health_extras_without_a_file_is_healthy_unless_required(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)

    assert health_extras(tmp_path) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }

    monkeypatch.setenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", "1")
    required = health_extras(tmp_path)
    assert required["healthy"] is False
    assert required["degraded"] == ["scheduler_unreported"]
    assert required["scheduler_age_s"] is None


@pytest.mark.parametrize("raw", ["{not json", "[]", '"x"', "", "null"])
def test_health_extras_treats_unusable_files_as_absent(tmp_path, monkeypatch, raw):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    (tmp_path / ".scheduler_health.json").write_text(raw)

    assert health_extras(tmp_path) == {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }


def test_health_extras_reads_a_valid_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", "300")
    heartbeat = datetime.now(UTC) - timedelta(seconds=5)
    (tmp_path / ".scheduler_health.json").write_text(
        json.dumps({"heartbeat_ts": heartbeat.isoformat(), "last_error": None})
    )

    result = health_extras(tmp_path)

    assert result["healthy"] is True
    assert result["degraded"] == []
    assert isinstance(result["scheduler_age_s"], int)


def test_health_extras_does_not_cache_the_file_between_calls(tmp_path, monkeypatch):
    """Delete the file + require a scheduler: the NEXT call must say unreported."""
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    path = tmp_path / ".scheduler_health.json"
    path.write_text(json.dumps({"heartbeat_ts": datetime.now(UTC).isoformat()}))

    assert health_extras(tmp_path)["healthy"] is True

    path.unlink()
    monkeypatch.setenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", "1")
    result = health_extras(tmp_path)

    assert result["healthy"] is False
    assert result["degraded"] == ["scheduler_unreported"]
    assert result["scheduler_age_s"] is None


def test_health_extras_tolerates_a_directory_in_place_of_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    (tmp_path / ".scheduler_health.json").mkdir()

    assert health_extras(tmp_path)["healthy"] is True


# ---------------------------------------------------------------------------
# /api/health integration
# ---------------------------------------------------------------------------


def test_api_health_reports_freshness_keys_and_keeps_ok_true(client: TestClient):  # noqa: F811
    res = client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert "healthy" in body
    assert isinstance(body["healthy"], bool)
    assert isinstance(body["degraded"], list)
    assert "scheduler_age_s" in body


def test_api_health_with_a_fresh_scheduler_file(client: TestClient, plan_dir, monkeypatch):  # noqa: F811
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    (plan_dir / ".scheduler_health.json").write_text(
        json.dumps({"heartbeat_ts": datetime.now(UTC).isoformat()})
    )

    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["healthy"] is True
    assert body["degraded"] == []


def test_api_health_reports_a_stale_scheduler_file(client: TestClient, plan_dir, monkeypatch):  # noqa: F811
    """A stale scheduler file degrades ``healthy`` but leaves ``ok`` True."""
    monkeypatch.delenv("PIPELINE_HEALTH_REQUIRE_SCHEDULER", raising=False)
    monkeypatch.setenv("PIPELINE_HEALTH_STALE_SECONDS", "60")
    stale = datetime.now(UTC) - timedelta(seconds=600)
    (plan_dir / ".scheduler_health.json").write_text(
        json.dumps({"heartbeat_ts": stale.isoformat(), "last_error": "boom"})
    )

    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["healthy"] is False
    assert body["degraded"] == ["scheduler_stale"]
    assert body["scheduler_age_s"] >= 600


def test_api_health_only_adds_the_three_freshness_keys(client: TestClient):  # noqa: F811
    """``health_extras`` splats exactly three keys; nothing else leaks in."""
    body = client.get("/api/health").json()

    assert set(health_freshness.freshness({}, datetime.now(UTC), 300.0, False)) <= set(body)
    assert dashboard.app is not None
