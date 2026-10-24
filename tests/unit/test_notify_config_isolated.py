"""The test session must never read a developer's real notifications.json.

Regression guard for NC-10: with a real PLAN_DIR/notifications.json present,
the suite spooled fixture events into the live per-instance queues.
"""

import json
import os
from pathlib import Path

from pipeline import notification_outbox, notification_routing, paths

CONFIG_PATH_ENV = "PIPELINE_NOTIFY_CONFIG_PATH"

_CONFIG = {
    "version": 1,
    "instances": {"ops": {"type": "slack", "webhook_url_env": "OPS_HOOK"}},
    "routes": [
        {"name": "all", "match": {"plan": "*"}, "deliver": [{"events": ["*"], "to": ["ops"]}]}
    ],
}


def test_should_set_session_config_path_to_a_nonexistent_file():
    configured = os.environ.get(CONFIG_PATH_ENV)

    assert configured
    assert not Path(configured).exists()


def test_should_ignore_a_real_config_in_plan_dir(tmp_path, monkeypatch):
    (tmp_path / "notifications.json").write_text(json.dumps(_CONFIG))
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)

    assert notification_routing.safe_load_notification_config() is None


def test_should_not_spool_instance_queues_for_a_real_config_in_plan_dir(tmp_path, monkeypatch):
    (tmp_path / "notifications.json").write_text(json.dumps(_CONFIG))
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(notification_outbox, "PLAN_DIR", tmp_path)

    notification_outbox.outbox_sink(
        {"type": "notification", "plan": "fixture", "payload": {"event": "story_merged"}}
    )

    legacy = f"fixture{notification_outbox.OUTBOX_SUFFIX}"
    spooled = [p.name for p in tmp_path.glob("*.outbox.*.jsonl") if p.name != legacy]
    assert spooled == []
