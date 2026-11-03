"""Contract test for the generated notification config and its docs (SN-03).

The standalone installer writes ``deploy/standalone/notification.json`` from the
operator's answers; this module pins the shape it writes, the validation the
runtime loader applies to it, and the documentation that describes it, so the
config and the docs cannot drift apart silently.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.notification_routing import (
    NotificationConfigError,
    load_notification_config,
)

README = Path(__file__).resolve().parents[2] / "deploy" / "README.md"

WEBHOOK_URL = "https://hooks.example.test/services/T/B/X"
EMAIL_PASSWORD = "test-password"

ENV = {
    "SLACK_WEBHOOK_URL": WEBHOOK_URL,
    "PIPELINE_NOTIFY_EMAIL_PASSWORD": EMAIL_PASSWORD,
}

INSTALLER_FLAGS = (
    "--notify-slack-webhook-file",
    "--notify-email-host",
    "--notify-email-port",
    "--notify-email-user",
    "--notify-email-from",
    "--notify-email-to",
    "--notify-email-password-file",
)

SECRET_PATHS = (
    "deploy/standalone/secrets/slack_webhook_url",
    "deploy/standalone/secrets/notify_email_password",
)


def installed_config() -> dict[str, Any]:
    """A fresh config equivalent to what ``deploy/install.sh`` generates.

    Only environment variable *names* appear for the secrets; the values live
    in the mounted ``0600`` secret files and are exported at container start.
    """
    return {
        "version": 1,
        "instances": {
            "email": {
                "type": "email",
                "host": "smtp.example.test",
                "port": 587,
                "user": "pipeline@example.test",
                "from": "pipeline@example.test",
                "to": ["oncall@example.test"],
                "password_env": "PIPELINE_NOTIFY_EMAIL_PASSWORD",
            },
            "slack": {
                "type": "slack",
                "webhook_url_env": "SLACK_WEBHOOK_URL",
            },
        },
        "routes": [
            {
                "name": "all",
                "match": {"plan": "*"},
                # "*" is what the installer writes: every event type is
                # delivered, including ones no code path emits yet.
                "deliver": [{"events": ["*"], "to": ["email", "slack"]}],
            }
        ],
    }


def write_config(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "notification.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_installer_config_loads_and_resolves_both_channels(tmp_path: Path) -> None:
    path = write_config(tmp_path, installed_config())

    config = load_notification_config(path, ENV)

    assert config is not None
    assert set(config.instances) == {"email", "slack"}
    assert sorted(config.resolve("any-plan", "plan_completed")) == ["email", "slack"]


def test_all_route_wildcard_delivers_unknown_events(tmp_path: Path) -> None:
    """The ``all`` route's ``events: ["*"]`` matches every event, known or not.

    This is the documented behaviour of the generated file: an event name the
    route does not enumerate is still delivered rather than silently dropped.
    """
    path = write_config(tmp_path, installed_config())
    config = load_notification_config(path, ENV)
    assert config is not None

    assert sorted(config.resolve("any-plan", "plan_completed")) == ["email", "slack"]
    assert sorted(config.resolve("any-plan", "unknown_event")) == ["email", "slack"]


def test_inline_webhook_url_is_rejected(tmp_path: Path) -> None:
    config = installed_config()
    config["instances"]["slack"]["webhook_url"] = WEBHOOK_URL

    with pytest.raises(NotificationConfigError):
        load_notification_config(write_config(tmp_path, config), ENV)


def test_route_naming_unknown_instance_is_rejected(tmp_path: Path) -> None:
    config = installed_config()
    config["routes"][0]["deliver"][0]["to"] = ["does-not-exist"]

    with pytest.raises(NotificationConfigError):
        load_notification_config(write_config(tmp_path, config), ENV)


def test_readme_documents_flags_and_secret_files() -> None:
    text = README.read_text(encoding="utf-8")

    missing = [flag for flag in INSTALLER_FLAGS if flag not in text]
    assert missing == [], f"deploy/README.md does not document {missing}"

    missing = [path for path in SECRET_PATHS if path not in text]
    assert missing == [], f"deploy/README.md does not document {missing}"


def test_base_config_is_not_shared_between_tests() -> None:
    """Each test builds its own config; the helper must not hand out one dict."""
    first = installed_config()
    second = installed_config()
    assert first == second
    assert first is not second
    assert copy.deepcopy(first) == second
    # Mutating a returned config must not leak into the next build.
    first["instances"]["slack"]["webhook_url"] = WEBHOOK_URL
    assert "webhook_url" not in installed_config()["instances"]["slack"]
