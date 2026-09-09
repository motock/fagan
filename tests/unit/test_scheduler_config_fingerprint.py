"""Story sh-03: the scheduler daemon must publish the config it resolved.

``SchedulerDaemon.config_fingerprint()`` is a NEW additive surface; the
existing ``health()`` dict must stay byte-for-byte the six keys pinned by
``test_scheduler_daemon.py::test_health_has_expected_keys``. These tests
enforce both halves: the new fingerprint exists, and ``health()`` did not
grow.
"""

import json
import os

import pytest

from pipeline import config as pipeline_config
from pipeline import paths as pipeline_paths
from pipeline.scheduler_daemon import SchedulerDaemon

# The exact key set pinned by tests/unit/test_scheduler_daemon.py::
# test_health_has_expected_keys. health() must never grow past this.
_HEALTH_KEYS = {
    "alive",
    "last_reconcile_ts",
    "last_scan_ts",
    "last_error",
    "reconcile_count",
    "scan_count",
}

_FINGERPRINT_KEYS = (
    "plan_dir",
    "worktree_root",
    "autonomy",
    "dispatch_backend",
    "pid",
)


class _FakeBus:
    """Stand-in for EventBus; none of the tested methods touch the bus."""


def _make_daemon(**kwargs):
    return SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=_FakeBus(),
        **kwargs,
    )


def test_config_fingerprint_returns_dict_with_required_keys():
    """config_fingerprint() is a dict containing every required key.

    Membership (not set equality): later stories may add fields.
    """
    daemon = _make_daemon()
    fingerprint = daemon.config_fingerprint()
    assert isinstance(fingerprint, dict)
    for key in _FINGERPRINT_KEYS:
        assert key in fingerprint, (
            f"config_fingerprint() is missing required key {key!r}; "
            f"got keys {sorted(fingerprint)}"
        )


def test_config_fingerprint_pid_is_current_process():
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["pid"] == os.getpid()


def test_config_fingerprint_autonomy_mirrors_pipeline_config():
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["autonomy"] == (
        pipeline_config.PIPELINE_AUTONOMY
    )


def test_config_fingerprint_plan_dir_read_live(tmp_path, monkeypatch):
    """plan_dir must be read at call time, not cached at import.

    Patching ``pipeline.paths.PLAN_DIR`` (the source module attribute) must
    be visible through config_fingerprint(); an implementer that did
    ``from pipeline.paths import PLAN_DIR`` at module import would fail this.
    """
    monkeypatch.setattr(pipeline_paths, "PLAN_DIR", str(tmp_path))
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["plan_dir"] == str(tmp_path)


def test_config_fingerprint_worktree_root_read_live(tmp_path, monkeypatch):
    """worktree_root is read live from pipeline.paths, same as plan_dir."""
    monkeypatch.setattr(pipeline_paths, "WORKTREE_ROOT", str(tmp_path))
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["worktree_root"] == str(tmp_path)


def test_config_fingerprint_dispatch_backend_none_when_env_absent(monkeypatch):
    """NEGATIVE: no PIPELINE_BACKEND_DISPATCH -> None, not '' nor raise."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["dispatch_backend"] is None


def test_config_fingerprint_dispatch_backend_reflects_env(monkeypatch):
    """POSITIVE: a set PIPELINE_BACKEND_DISPATCH is surfaced verbatim."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "fake-backend")
    daemon = _make_daemon()
    assert daemon.config_fingerprint()["dispatch_backend"] == "fake-backend"


def test_health_keys_are_exactly_the_pinned_six():
    """REGRESSION: health() must not widen because of the fingerprint work.

    This is the point of the story — assert exact set equality here so the
    additive ``config`` payload can never leak into health().
    """
    daemon = _make_daemon()
    assert set(daemon.health().keys()) == _HEALTH_KEYS


def test_write_health_json_has_health_keys_plus_config(tmp_path):
    """write_health() emits the six health keys AND a top-level 'config'."""
    daemon = _make_daemon()
    path = tmp_path / "health.json"
    daemon.write_health(str(path))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in _HEALTH_KEYS:
        assert key in data, f"write_health() dropped health key {key!r}"
    assert "config" in data, (
        "write_health() must add a top-level 'config' key holding "
        "config_fingerprint()"
    )
    assert isinstance(data["config"], dict)


def test_write_health_config_matches_config_fingerprint(tmp_path):
    """The 'config' payload in the file equals config_fingerprint()."""
    daemon = _make_daemon()
    path = tmp_path / "health.json"
    daemon.write_health(str(path))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["config"] == daemon.config_fingerprint()


def test_write_health_leaves_no_tmp_file(tmp_path):
    """Atomic-write mechanics are unchanged: no <path>.tmp left behind."""
    daemon = _make_daemon()
    path = tmp_path / "health.json"
    daemon.write_health(str(path))
    assert os.path.exists(str(path))
    assert not os.path.exists(str(path) + ".tmp")


def test_health_unchanged_after_write_health(tmp_path):
    """Calling write_health() must not mutate health()'s key set."""
    daemon = _make_daemon()
    daemon.write_health(str(tmp_path / "health.json"))
    assert set(daemon.health().keys()) == _HEALTH_KEYS


def test_config_fingerprint_is_json_serializable():
    """The fingerprint must survive json round-trip (write_health dumps it)."""
    daemon = _make_daemon()
    fingerprint = daemon.config_fingerprint()
    assert json.loads(json.dumps(fingerprint)) == fingerprint


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))