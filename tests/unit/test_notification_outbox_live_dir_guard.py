"""Defence-in-depth guard: never spool instance notifications into the live
plan directory while running under pytest.

NC-10 pointed the whole pytest session at a nonexistent notification config,
but a test that sets ``PIPELINE_NOTIFY_CONFIG_PATH`` itself (or whose fixture
resets the environment) could still spool fixture events into the operator's
real e-mail/Slack queues.  ``_spool_instances`` therefore refuses to write when
``PYTEST_CURRENT_TEST`` is set *and* ``PLAN_DIR`` resolves to the real default
plan directory.
"""

import json
from pathlib import Path

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline import paths
from pipeline.notification_channels import DeliveryResult, DeliveryStatus

PLAN = "ng01-plan"

CONFIG = {
    "version": 1,
    "instances": {"email-a": {"type": "fake_ng01"}},
    "routes": [
        {
            "name": "all",
            "match": {"plan": "ng01-*"},
            "deliver": [{"events": ["plan_completed"], "to": ["email-a"]}],
        }
    ],
}


class FakeChannel:
    """Minimal channel: only the attributes the spooler touches."""

    type_name = "fake_ng01"

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        return cls(instance_name)

    def send(self, record):  # pragma: no cover - never drained here
        return DeliveryResult(DeliveryStatus.DELIVERED)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """A valid config routing ``plan_completed`` to one instance."""
    monkeypatch.setattr(nc, "_REGISTRY", {"fake_ng01": FakeChannel})
    cfg = tmp_path / "cfg" / "notifications.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(CONFIG))
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(cfg))
    return cfg


def _emit():
    ob.outbox_sink(
        {"type": "notification", "plan": PLAN, "payload": {"event": "plan_completed"}}
    )


def _queue_file(plan_dir):
    return plan_dir / f"{PLAN}.outbox.email-a.jsonl"


def test_guard_blocks_when_pytest_and_default_plan_dir(
    tmp_path, monkeypatch, config_path
):
    tmp_home = tmp_path / "home"
    tmp_home.mkdir()
    # Patch home FIRST so the default plan dir below is the fake one.
    monkeypatch.setattr(Path, "home", lambda: tmp_home)
    live_plans = Path.home() / ".claude" / "plans"
    live_plans.mkdir(parents=True)
    monkeypatch.setattr(ob, "PLAN_DIR", live_plans)
    # The write path resolves through ``paths.PLAN_DIR``; point it at the same
    # fake directory so a missing guard is observable and nothing real is hit.
    monkeypatch.setattr(paths, "PLAN_DIR", live_plans)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_guard_blocks (call)")

    _emit()

    assert not _queue_file(live_plans).exists()


def test_guard_allows_tmp_plan_dir_under_pytest(tmp_path, monkeypatch, config_path):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_guard_allows_tmp (call)")
    monkeypatch.setattr(ob, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)

    _emit()

    assert _queue_file(tmp_path).exists()


def test_guard_allows_when_pytest_unset(tmp_path, monkeypatch, config_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ob, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)

    _emit()

    assert _queue_file(tmp_path).exists()
