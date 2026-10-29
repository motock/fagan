"""SQ-08: cross-process wake markers consumed by the scheduler scan.

Operator actions (approve, resume, ingest, set status, patch, mark done) run in
the MCP server or the dashboard, not in the scheduler, so an in-process bus
event cannot reach the scheduler's handlers on the default bus. A marker file
per plan works on every bus, coalesces naturally, and follows the
claim-by-rename idiom ``scan_done_markers`` already uses.

The contract pinned here:

* ``request_wake`` writes a marker atomically and never raises -- a lost wake
  costs at most one stale-plan sweep;
* ``claim_wakes`` claims by rename, so each marker is delivered at most once
  even with concurrent passes, and untrusted names are left untouched;
* the marker directory/file modes keep other local users out;
* ``scan_all_plans`` turns each claimed marker into one ``reconcile`` event.
"""

import json
import logging
import os
import stat
import sys

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle (see test_triage_cap_silent_skip)
from pipeline import paths, wake, watchers

_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_WINDOWS = sys.platform.startswith("win")
# chmod-based read-only dirs are meaningless for root (root ignores perms) and
# unsupported on Windows.
_READONLY_UNTESTABLE = _ROOT or _WINDOWS


class FakeBus:
    """Records the events published by the scan."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _wake_dir(tmp_path):
    return tmp_path / ".wake"


def _point_plan_dir_at(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# request_wake + claim_wakes round trip
# --------------------------------------------------------------------------- #


def test_request_wake_then_claim_returns_plan_once(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    wake.request_wake("alpha", "approve")
    assert wake.claim_wakes() == ["alpha"]
    assert wake.claim_wakes() == []


def test_marker_holds_reason_and_iso_timestamp(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    wake.request_wake("alpha", "approve")

    payload = json.loads((_wake_dir(tmp_path) / "alpha").read_text())
    assert payload["reason"] == "approve"
    assert "T" in payload["ts"]


def test_two_requests_before_a_claim_coalesce(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    wake.request_wake("alpha", "approve")
    wake.request_wake("alpha", "resume")

    assert wake.claim_wakes() == ["alpha"]
    assert wake.claim_wakes() == []


def test_claim_without_wake_dir_returns_nothing(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    assert wake.claim_wakes() == []


def test_claim_does_not_trust_marker_contents(tmp_path, monkeypatch):
    """Only the validated file name matters; a junk body is still claimed."""
    _point_plan_dir_at(tmp_path, monkeypatch)
    _wake_dir(tmp_path).mkdir(mode=0o700)
    (_wake_dir(tmp_path) / "alpha").write_text("{not json")

    assert wake.claim_wakes() == ["alpha"]


# --------------------------------------------------------------------------- #
# Modes: the wake dir and markers stay private to this user.
# --------------------------------------------------------------------------- #


def test_wake_dir_is_private_and_marker_is_owner_only(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    wake.request_wake("alpha", "approve")

    marker = _wake_dir(tmp_path) / "alpha"
    assert stat.S_IMODE(_wake_dir(tmp_path).stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Negative paths: a lost wake is acceptable, a raised exception is not.
# --------------------------------------------------------------------------- #


def test_request_wake_rejects_traversal_without_writing(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)

    wake.request_wake("../x", "approve")

    assert list(tmp_path.iterdir()) == []


def test_request_wake_rejects_traversal_without_raising(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)
    _wake_dir(tmp_path).mkdir(mode=0o700)
    _wake_dir(tmp_path).joinpath("sentinel").write_text("keep")

    wake.request_wake("../x", "approve")

    # The pre-existing marker is untouched and no traversal file appeared.
    assert [p.name for p in _wake_dir(tmp_path).iterdir()] == ["sentinel"]


@pytest.mark.skipif(_READONLY_UNTESTABLE, reason="chmod 0o500 is meaningless for root / on Windows")
def test_unwritable_plan_dir_logs_warning_and_does_not_raise(tmp_path, monkeypatch, caplog):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_dir.chmod(0o500)
    _point_plan_dir_at(plan_dir, monkeypatch)

    try:
        with caplog.at_level(logging.WARNING, logger="pipeline.wake"):
            wake.request_wake("alpha", "approve")
    finally:
        plan_dir.chmod(0o700)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a lost wake must be logged"
    assert warnings[0].plan == "alpha"
    # Secure-by-Design: the log names the plan, never the filesystem path.
    assert str(plan_dir) not in warnings[0].getMessage()


def test_claim_skips_temp_and_invalid_names_and_leaves_them_on_disk(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)
    wake_dir = _wake_dir(tmp_path)
    wake_dir.mkdir(mode=0o700)
    (wake_dir / ".alpha.123.tmp").write_text("{}")
    (wake_dir / "bad name").write_text("{}")
    (wake_dir / "alpha").write_text("{}")

    assert wake.claim_wakes() == ["alpha"]
    assert sorted(p.name for p in wake_dir.iterdir()) == [".alpha.123.tmp", "bad name"]


# --------------------------------------------------------------------------- #
# after(): one-line wake on a successful operator result.
# --------------------------------------------------------------------------- #


def test_after_wakes_on_success_and_returns_result_unchanged(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)
    result = {"ok": True, "status": "done"}

    returned = wake.after("alpha", "mark_done", result)

    assert returned is result
    assert wake.claim_wakes() == ["alpha"]


def test_after_skips_failed_or_skipped_results(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)
    failed = {"ok": False}
    skipped = {"ok": True, "skipped": "already done"}

    assert wake.after("alpha", "approve", failed) is failed
    assert wake.after("alpha", "approve", skipped) is skipped
    assert wake.after("alpha", "approve", "not a dict") == "not a dict"
    assert wake.claim_wakes() == []


# --------------------------------------------------------------------------- #
# scan_all_plans consumes the markers
# --------------------------------------------------------------------------- #


def _write_manifest(plan_dir, plan_name):
    manifest = {"plan": plan_name, "stories": {}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def test_scan_all_plans_publishes_one_reconcile_per_claimed_plan(tmp_path, monkeypatch):
    _point_plan_dir_at(tmp_path, monkeypatch)
    _write_manifest(tmp_path, "alpha")
    wake.request_wake("alpha", "approve")
    wake.request_wake("beta", "resume")
    bus = FakeBus()

    watchers.scan_all_plans(bus)

    reconciles = [e for e in bus.events if e.get("type") == "reconcile"]
    assert sorted(e["plan"] for e in reconciles) == ["alpha", "beta"]
    assert all(e["payload"] == {"reason": "requested"} for e in reconciles)
    # The markers were consumed: a second sweep publishes nothing new.
    bus.events.clear()
    watchers.scan_all_plans(bus)
    assert [e for e in bus.events if e.get("type") == "reconcile"] == []


def test_scan_all_plans_survives_a_failing_wake_claim(tmp_path, monkeypatch, caplog):
    _point_plan_dir_at(tmp_path, monkeypatch)
    _write_manifest(tmp_path, "alpha")

    def _boom():
        raise OSError("unreadable")

    monkeypatch.setattr(wake, "claim_wakes", _boom)
    bus = FakeBus()

    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        events = watchers.scan_all_plans(bus)

    assert events == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)
