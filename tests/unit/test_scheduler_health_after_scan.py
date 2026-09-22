"""Health must also be written at the end of the scan phase.

Written only at tick end, a daemon stuck in a later phase (reconcile, drain)
reports a scan_count frozen at the previous complete tick -- indistinguishable
from a dead daemon.
"""
import json

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus


def test_health_on_disk_already_reflects_the_scan_when_reconcile_runs(tmp_path):
    health_path = tmp_path / "health.json"
    seen: dict = {}

    def reconcile():
        if health_path.exists():
            with open(health_path) as fh:
                seen.update(json.load(fh))
        else:
            seen["absent"] = True

    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=0,
        sleep_fn=lambda _s: None,
        health_path=str(health_path),
    )
    daemon.run_once()

    assert "absent" not in seen, (
        "no health file was on disk while reconcile ran; it is still written "
        "only once the whole tick finishes"
    )
    assert seen["scan_count"] >= 1, (
        "the on-disk health file predates the scan that just completed"
    )


def test_mid_tick_write_stays_gated_on_health_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mod.SchedulerDaemon, "write_health", lambda self, path: calls.append(path)
    )
    daemon = mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=0,
        sleep_fn=lambda _s: None,
    )
    daemon.run_once()
    assert calls == []
