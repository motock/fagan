"""Integration wiring: the REAL producer against the REAL consumer.

The dashboard-side tests in ``test_dashboard_health_config_mismatch.py``
hand-construct a fake ``.scheduler_health.json`` via a ``_write_fingerprint``
helper — they never exercise the real production write path
(``SchedulerDaemon.write_health``) against the real consumer
(``app.dashboard.health()`` via ``/api/health``). That gap is exactly how
CFG-B5's bug shipped undetected: ``write_health()`` never put the config
fingerprint in the file, so ``/api/health`` always reported
``scheduler=null`` / ``config_mismatch=[]`` no matter what the two
processes resolved.

These tests drive both halves together: a real ``SchedulerDaemon`` writes
the health file through the real production code path, and the dashboard's
TestClient reads it back through the real ``/api/health`` route.
"""

from __future__ import annotations

import json

import pipeline.paths
import pipeline.server
from pipeline.scheduler_daemon import SchedulerDaemon
from tests.unit._dashboard_helpers import client  # noqa: F401

HEALTH_FILENAME = ".scheduler_health.json"


class _FakeBus:
    """Stand-in for EventBus; none of the tested methods touch the bus."""


def _make_daemon() -> SchedulerDaemon:
    """A real SchedulerDaemon with no-op reconcile/scan fns (CFG-B1 pattern)."""
    return SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=_FakeBus(),
    )


def _patch_both_sides(monkeypatch, plan_dir, worktree_root) -> None:
    """Point BOTH sides' read-sites at the same tmp directories.

    The daemon's ``config_fingerprint()`` reads ``pipeline.paths.PLAN_DIR`` /
    ``pipeline.paths.WORKTREE_ROOT`` lazily at call time; the dashboard's
    ``app.dashboard.PLAN_DIR`` / ``WORKTREE_ROOT`` are ``LiveRef``s that
    resolve ``pipeline.server.<name>`` on every access. Patching both
    modules (the ``canonical_dirs`` pattern) makes the two processes resolve
    identical config.
    """
    for module in (pipeline.server, pipeline.paths):
        monkeypatch.setattr(module, "PLAN_DIR", plan_dir)
        monkeypatch.setattr(module, "WORKTREE_ROOT", worktree_root)


def test_real_write_health_feeds_real_api_health_when_configs_agree(
    tmp_path, monkeypatch, client
):
    """A real write_health() file makes /api/health report the fingerprint.

    The daemon and the dashboard are pointed at the same plan dir and
    worktree root, so the dashboard must surface the scheduler's config
    fingerprint (not ``None``) and report no mismatch.
    """
    plan_dir = tmp_path / "plans"
    worktree_root = tmp_path / "wt_a"
    plan_dir.mkdir()
    worktree_root.mkdir()
    _patch_both_sides(monkeypatch, plan_dir, worktree_root)

    daemon = _make_daemon()
    health_path = str(plan_dir / HEALTH_FILENAME)
    # The REAL production write path — not a hand-built dict.
    daemon.write_health(health_path)

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["scheduler"] is not None, (
        "the real write_health() file must carry a config fingerprint the "
        "dashboard can surface; scheduler=null means the wiring is broken"
    )
    assert body["scheduler"] == daemon.config_fingerprint()
    assert body["config_mismatch"] == []


def test_real_write_health_feeds_real_api_health_when_configs_diverge(
    tmp_path, monkeypatch, client
):
    """A diverged dashboard resolution is reported via config_mismatch.

    Both sides start at ``wt_a`` and the daemon writes the health file.
    Then ONLY the dashboard's read-site (the ``pipeline.server`` mirror its
    LiveRefs resolve) is repointed at ``wt_b`` — the daemon keeps reading
    ``pipeline.paths.WORKTREE_ROOT`` (``wt_a``), so its follow-up
    ``write_health()`` must write the SAME fingerprint as before (its
    inputs were untouched) and the dashboard must now report the
    ``worktree_root`` divergence.
    """
    plan_dir = tmp_path / "plans"
    wt_a = tmp_path / "wt_a"
    wt_b = tmp_path / "wt_b"
    plan_dir.mkdir()
    wt_a.mkdir()
    wt_b.mkdir()
    _patch_both_sides(monkeypatch, plan_dir, wt_a)

    daemon = _make_daemon()
    health_path = str(plan_dir / HEALTH_FILENAME)
    daemon.write_health(health_path)

    # Diverge ONLY the dashboard's fingerprint read-site. The daemon's
    # read-site (pipeline.paths.WORKTREE_ROOT) stays at wt_a.
    monkeypatch.setattr(pipeline.server, "WORKTREE_ROOT", wt_b)
    # Follow-up call: the file and patches persist across calls, so this
    # second write must be identical to the first — the daemon's inputs
    # did not change.
    daemon.write_health(health_path)

    with open(health_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["config"] == daemon.config_fingerprint(), (
        "the daemon's second write must still carry ITS OWN fingerprint "
        "(wt_a); if it carries wt_b the divergence patch leaked into the "
        "daemon's read-site and the test is not testing divergence"
    )
    assert on_disk["config"]["worktree_root"] == str(wt_a)

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert "worktree_root" in body["config_mismatch"], (
        "the dashboard resolves wt_b while the file's fingerprint says "
        f"wt_a; config_mismatch was {body['config_mismatch']!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))