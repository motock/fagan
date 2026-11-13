"""Configuration readers resolve the plan directory through one accessor.

Story store-c2-r12: modules that only report or resolve configuration call
``pipeline.paths.configured_plan_dir()`` (and ``default_notification_config_path()``)
instead of reading ``PLAN_DIR`` themselves. The accessor reads the module
attribute at call time, so a monkeypatched ``paths.PLAN_DIR`` is always seen.
Preflight's scheduler fingerprint is read through ``FileHostState``, rooted at
the plan directory the caller resolved.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import host_state, notification_routing, paths, preflight
from pipeline.topology_identity import resolved_topology

REFERENCE_DOC = Path(__file__).resolve().parents[2] / "REFERENCE.md"


def _ok_which(name: str):
    return f"/usr/local/bin/{name}"


def _ok_registry() -> dict:
    return {"models": [], "roles": {}}


def _scheduler_entry(results: list, name: str) -> dict:
    return next(r for r in results if r["name"] == name)


def _run_preflight(plan_dir=None):
    return preflight.run_preflight(
        plan_dir=plan_dir, which=_ok_which, registry_loader=_ok_registry
    )


def test_accessor_follows_a_monkeypatched_paths_plan_dir(tmp_path, monkeypatch):
    target = tmp_path / "plans"
    monkeypatch.setattr(paths, "PLAN_DIR", target)

    assert paths.configured_plan_dir() == target


def test_accessor_returns_a_path_not_a_string():
    assert isinstance(paths.configured_plan_dir(), Path)


def test_default_notification_config_path_is_notifications_json_in_plan_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)

    assert paths.default_notification_config_path() == tmp_path / "notifications.json"


def test_reference_doc_pins_notifications_json_location():
    assert "`PLAN_DIR/notifications.json`" in REFERENCE_DOC.read_text(encoding="utf-8")


def test_notification_config_path_prefers_explicit_path_over_env_and_default(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "default")
    env = {notification_routing.CONFIG_PATH_ENV: str(tmp_path / "env.json")}

    assert notification_routing._config_path(tmp_path / "explicit.json", env) == (
        tmp_path / "explicit.json"
    )


def test_notification_config_path_prefers_env_over_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "default")
    env = {notification_routing.CONFIG_PATH_ENV: str(tmp_path / "env.json")}

    assert notification_routing._config_path(None, env) == tmp_path / "env.json"


def test_notification_config_path_falls_back_to_plan_dir_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "default")

    assert notification_routing._config_path(None, {}) == (
        tmp_path / "default" / "notifications.json"
    )


def test_topology_plan_dir_reports_the_patched_plan_dir(tmp_path, monkeypatch):
    target = tmp_path / "plans"
    monkeypatch.setattr(paths, "PLAN_DIR", target)

    assert resolved_topology()["plan_dir"] == str(target)


def test_default_plan_dir_check_uses_the_configured_plan_dir(tmp_path, monkeypatch):
    target = tmp_path / "not-yet-created"
    monkeypatch.delenv("PLAN_DIR", raising=False)
    monkeypatch.setattr(paths, "PLAN_DIR", target)

    entry = _scheduler_entry(_run_preflight(), "PLAN_DIR")

    assert entry["status"] == "warn"
    assert str(target) in entry["message"]


def test_explicit_plan_dir_argument_still_wins_for_the_plan_dir_check(
    tmp_path, monkeypatch
):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "from-env"))
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "from-config")

    entry = _scheduler_entry(_run_preflight(plan_dir=explicit), "PLAN_DIR")

    assert entry["status"] == "ok"
    assert str(explicit) in entry["message"]


def test_scheduler_config_match_is_read_through_host_state(tmp_path, monkeypatch):
    """The fingerprint comes from HostState, not a direct open() of the file."""
    monkeypatch.delenv("WORKTREE_ROOT", raising=False)
    matching = {"plan_dir": str(tmp_path), "worktree_root": None}
    monkeypatch.setattr(
        host_state.FileHostState,
        "read_scheduler_health",
        lambda self: {"config": {**matching, "plan_dir": str(self.root())}},
    )

    entry = _scheduler_entry(_run_preflight(plan_dir=tmp_path), "SCHEDULER_CONFIG")

    assert entry["status"] == "ok"
    assert entry["message"].startswith("scheduler config matches resolved plan dir")


def test_scheduler_config_mismatch_names_both_plan_dirs(tmp_path):
    other = tmp_path / "elsewhere"
    (tmp_path / ".scheduler_health.json").write_text(
        json.dumps({"config": {"plan_dir": str(other), "worktree_root": None}}),
        encoding="utf-8",
    )

    entry = _scheduler_entry(_run_preflight(plan_dir=tmp_path), "SCHEDULER_CONFIG")

    assert entry["status"] == "warn"
    assert f"plan_dir: fingerprint={other} resolved={tmp_path}" in entry["message"]


def test_missing_fingerprint_reads_as_ok_not_malformed(tmp_path):
    entry = _scheduler_entry(_run_preflight(plan_dir=tmp_path), "SCHEDULER_CONFIG")

    assert entry["status"] == "ok"
    assert "no scheduler fingerprint found" in entry["message"]


def test_malformed_fingerprint_reads_as_warn(tmp_path):
    (tmp_path / ".scheduler_health.json").write_text("{not json", encoding="utf-8")

    entry = _scheduler_entry(_run_preflight(plan_dir=tmp_path), "SCHEDULER_CONFIG")

    assert entry["status"] == "warn"
    assert "malformed or unreadable" in entry["message"]
