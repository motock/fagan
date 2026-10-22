"""Tests for the guided-setup notification projections.

``pipeline.setup.notifications`` exposes two pure projections that turn
prompt answers into the env changes the setup writer persists. These tests
pin the exact key sets, the ``"1"``/``"0"`` gate encoding, the aggregated
enabled-email error, and the guarantee that a secret never reaches an
exception message.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pipeline.setup.notifications import build_email_settings, build_outbox_settings

from pipeline.notification_outbox import DEFAULT_OUTBOX_EVENTS

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "pipeline" / "setup" / "notifications.py"
)

OUTBOX_KEYS = {
    "PIPELINE_NOTIFY_OUTBOX_ENABLED",
    "PIPELINE_NOTIFY_OUTBOX_EVENTS",
}

EMAIL_KEYS = {
    "PIPELINE_NOTIFY_EMAIL_ENABLED",
    "PIPELINE_NOTIFY_EMAIL_HOST",
    "PIPELINE_NOTIFY_EMAIL_PORT",
    "PIPELINE_NOTIFY_EMAIL_USER",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD",
    "PIPELINE_NOTIFY_EMAIL_FROM",
    "PIPELINE_NOTIFY_EMAIL_TO",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT",
}

PASSWORD_SENTINEL = "s3cr3t-SENTINEL-do-not-leak"

COMPLETE_EMAIL = {
    "enabled": True,
    "host": "smtp.example.com",
    "port": "587",
    "user": "pipeline",
    "password": PASSWORD_SENTINEL,
    "sender": "pipeline@example.com",
    "recipients": "ops@example.com",
    "timeout": "20",
}


# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------


def test_disabled_outbox_returns_gate_zero_and_exact_keys():
    result = build_outbox_settings(False)

    assert set(result) == OUTBOX_KEYS
    assert result["PIPELINE_NOTIFY_OUTBOX_ENABLED"] == "0"


def test_enabled_outbox_returns_gate_one():
    result = build_outbox_settings(True, ["plan_completed"])

    assert result["PIPELINE_NOTIFY_OUTBOX_ENABLED"] == "1"


def test_enabled_outbox_normalizes_event_list():
    result = build_outbox_settings(True, ["plan_completed", "story_parked"])

    assert result["PIPELINE_NOTIFY_OUTBOX_EVENTS"] == "plan_completed,story_parked"


def test_outbox_accepts_comma_separated_string_with_spaces():
    result = build_outbox_settings(True, "plan_completed, story_parked")

    assert result["PIPELINE_NOTIFY_OUTBOX_EVENTS"] == "plan_completed,story_parked"


def test_outbox_none_events_uses_default_outbox_events():
    result = build_outbox_settings(True, None)

    assert result["PIPELINE_NOTIFY_OUTBOX_EVENTS"] == DEFAULT_OUTBOX_EVENTS


def test_enabled_email_complete_returns_every_key_and_preserves_password():
    result = build_email_settings(**COMPLETE_EMAIL)

    assert set(result) == EMAIL_KEYS
    assert result["PIPELINE_NOTIFY_EMAIL_ENABLED"] == "1"
    assert result["PIPELINE_NOTIFY_EMAIL_HOST"] == "smtp.example.com"
    assert result["PIPELINE_NOTIFY_EMAIL_PORT"] == "587"
    assert result["PIPELINE_NOTIFY_EMAIL_USER"] == "pipeline"
    assert result["PIPELINE_NOTIFY_EMAIL_PASSWORD"] == PASSWORD_SENTINEL
    assert result["PIPELINE_NOTIFY_EMAIL_FROM"] == "pipeline@example.com"
    assert result["PIPELINE_NOTIFY_EMAIL_TO"] == "ops@example.com"
    assert result["PIPELINE_NOTIFY_EMAIL_TIMEOUT"] == "20"


def test_disabled_email_gate_is_zero():
    result = build_email_settings(enabled=False)

    assert set(result) == EMAIL_KEYS
    assert result["PIPELINE_NOTIFY_EMAIL_ENABLED"] == "0"


def test_single_recipient_normalizes():
    result = build_email_settings(**{**COMPLETE_EMAIL, "recipients": " ops@example.com "})

    assert result["PIPELINE_NOTIFY_EMAIL_TO"] == "ops@example.com"


def test_multi_recipient_list_normalizes():
    result = build_email_settings(
        **{**COMPLETE_EMAIL, "recipients": ["ops@example.com", "oncall@example.com"]}
    )

    assert result["PIPELINE_NOTIFY_EMAIL_TO"] == "ops@example.com,oncall@example.com"


def test_multi_recipient_string_normalizes():
    result = build_email_settings(
        **{**COMPLETE_EMAIL, "recipients": "ops@example.com, oncall@example.com"}
    )

    assert result["PIPELINE_NOTIFY_EMAIL_TO"] == "ops@example.com,oncall@example.com"


# ---------------------------------------------------------------------------
# Negative
# ---------------------------------------------------------------------------


def test_enabled_email_missing_required_lists_all_three_keys():
    with pytest.raises(ValueError) as excinfo:
        build_email_settings(enabled=True, host="", sender="", recipients="")

    message = str(excinfo.value)
    assert "PIPELINE_NOTIFY_EMAIL_HOST" in message
    assert "PIPELINE_NOTIFY_EMAIL_FROM" in message
    assert "PIPELINE_NOTIFY_EMAIL_TO" in message


def test_enabled_email_bad_port_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_PORT"):
        build_email_settings(**{**COMPLETE_EMAIL, "port": "smtp"})


def test_enabled_email_zero_timeout_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_TIMEOUT"):
        build_email_settings(**{**COMPLETE_EMAIL, "timeout": "0"})


def test_enabled_email_bad_sender_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_FROM"):
        build_email_settings(**{**COMPLETE_EMAIL, "sender": "not-an-email"})


def test_enabled_email_bad_recipient_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_TO"):
        build_email_settings(**{**COMPLETE_EMAIL, "recipients": "not-an-email"})


def test_outbox_bad_token_raises_naming_the_token():
    with pytest.raises(ValueError) as excinfo:
        build_outbox_settings(True, ["Bad-Token"])

    assert "Bad-Token" in str(excinfo.value)


def test_password_never_appears_in_enabled_error_text():
    with pytest.raises(ValueError) as excinfo:
        build_email_settings(**{**COMPLETE_EMAIL, "port": "smtp"})

    assert PASSWORD_SENTINEL not in str(excinfo.value)


def test_password_never_appears_in_disabled_error_text():
    with pytest.raises(ValueError) as excinfo:
        build_email_settings(enabled=False, port="smtp", password=PASSWORD_SENTINEL)

    assert PASSWORD_SENTINEL not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", ["1", "65535"])
def test_port_boundaries_accepted(port):
    result = build_email_settings(**{**COMPLETE_EMAIL, "port": port})

    assert result["PIPELINE_NOTIFY_EMAIL_PORT"] == port


@pytest.mark.parametrize("port", ["0", "65536"])
def test_port_out_of_range_rejected(port):
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_PORT"):
        build_email_settings(**{**COMPLETE_EMAIL, "port": port})


def test_timeout_one_accepted():
    result = build_email_settings(**{**COMPLETE_EMAIL, "timeout": "1"})

    assert result["PIPELINE_NOTIFY_EMAIL_TIMEOUT"] == "1"


def test_empty_events_string_falls_back_to_default():
    result = build_outbox_settings(True, "")

    assert result["PIPELINE_NOTIFY_OUTBOX_EVENTS"] == DEFAULT_OUTBOX_EVENTS


def test_empty_event_iterable_falls_back_to_default():
    result = build_outbox_settings(True, [])

    assert result["PIPELINE_NOTIFY_OUTBOX_EVENTS"] == DEFAULT_OUTBOX_EVENTS


def test_disabled_email_with_missing_required_values_is_accepted():
    result = build_email_settings(enabled=False)

    assert result["PIPELINE_NOTIFY_EMAIL_ENABLED"] == "0"
    assert result["PIPELINE_NOTIFY_EMAIL_HOST"] == ""
    assert result["PIPELINE_NOTIFY_EMAIL_FROM"] == ""
    assert result["PIPELINE_NOTIFY_EMAIL_TO"] == ""


def test_disabled_email_with_broken_port_still_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_PORT"):
        build_email_settings(enabled=False, port="smtp")


def test_disabled_email_with_broken_sender_still_raises():
    with pytest.raises(ValueError, match="PIPELINE_NOTIFY_EMAIL_FROM"):
        build_email_settings(enabled=False, sender="not-an-email")


# ---------------------------------------------------------------------------
# Purity / drift guards
# ---------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def test_module_reads_no_environ_and_does_no_io():
    tree = _module_tree()

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"socket", "subprocess", "smtplib", "urllib", "http", "requests"}

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            assert node.attr not in {"environ", "getenv", "putenv", "environb"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print", "input"}


def test_module_does_not_duplicate_the_default_outbox_literal():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert DEFAULT_OUTBOX_EVENTS not in source
