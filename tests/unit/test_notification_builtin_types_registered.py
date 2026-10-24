"""Built-in channel types must register when the config loads, whoever imported what."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[2])
from pipeline.notification_routing import NotificationConfigError, load_notification_config
try:
    cfg = load_notification_config(sys.argv[1], env={})
except NotificationConfigError as exc:
    print("ERR:" + str(exc))
else:
    print("OK:" + ",".join(sorted(cfg.resolve("p", "story_done"))))
"""


def _load_in_fresh_interpreter(tmp_path, config):
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT, str(path), str(REPO_ROOT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_should_register_email_and_slack_types_on_config_load(tmp_path):
    config = {
        "version": 1,
        "instances": {
            "mail": {
                "type": "email",
                "host": "smtp.example.com",
                "from": "a@example.com",
                "to": ["b@example.com"],
                "password_env": "SMTP_PASSWORD",
            },
            "chat": {"type": "slack", "webhook_url_env": "SLACK_WEBHOOK_URL"},
        },
        "routes": [
            {
                "name": "all",
                "match": {"plan": "*"},
                "deliver": [{"events": ["*"], "to": ["mail", "chat"]}],
            }
        ],
    }

    assert _load_in_fresh_interpreter(tmp_path, config) == "OK:chat,mail"


def test_should_still_reject_unknown_channel_type(tmp_path):
    config = {
        "version": 1,
        "instances": {"x": {"type": "carrierpigeon"}},
        "routes": [],
    }

    out = _load_in_fresh_interpreter(tmp_path, config)

    assert out.startswith("ERR:")
    assert "unknown channel type" in out
