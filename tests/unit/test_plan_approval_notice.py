"""Plan-approval notice builder and humanized approval subjects (PAN-02)."""

import json
import logging

import pytest
from pipeline.plan_approval_notice import (
    APPROVAL_EVENTS,
    approval_notice_message,
    notify_approval_event,
)

from pipeline.notification_render import render_notification
from pipeline.notification_routing import load_notification_config

DIGEST = "a1b2c3d4e5f6a7b8c9d0"  # first 12 hex: a1b2c3d4e5f6

SENTINELS = (
    "SENTINEL-AGENT-INSTRUCTIONS",
    "SENTINEL-SUMMARY",
    "SENTINEL-FILES",
    "SENTINEL-REPO-ROOT",
    "SENTINEL-ROLE-CONFIG",
    "SENTINEL-CONTEXT",
    "SENTINEL-REASON",
)


def _manifest():
    return {
        "plan": "p1",
        "repo_root": SENTINELS[3],
        "role_config": SENTINELS[4],
        "context": {"note": SENTINELS[5]},
        "approval": {
            "state": "pending_approval",
            "digest": DIGEST,
            "decided_by": "alice@example.com",
            "self_approved": False,
            "reason": SENTINELS[6],
        },
        "stories": {
            # No warnings: plain stories with no acceptance risk markers.
            "S-1": {
                "agent_instructions": SENTINELS[0],
                "summary": SENTINELS[1],
                "files": [SENTINELS[2]],
                "risk": "high",
                "status": "todo",
            },
            "S-2": {"risk": "low", "status": "todo"},
            "S-3": {"status": "todo"},
        },
    }


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PIPELINE_APPROVAL_LINK_PATH", raising=False)


def test_approval_events_constant():
    assert APPROVAL_EVENTS == (
        "plan_pending_approval",
        "plan_approved",
        "plan_changes_requested",
    )


def test_pending_approval_message(clean_env):
    assert approval_notice_message("p1", _manifest(), "plan_pending_approval") == (
        "Plan p1 is awaiting approval: 3 stories (risk: 1 high, 0 medium, 1 low, "
        "1 unset), 0 warnings, digest a1b2c3d4e5f6."
    )


def test_approved_message(clean_env):
    assert approval_notice_message("p1", _manifest(), "plan_approved") == (
        "Plan p1 was approved by alice@example.com (self-approved: no), "
        "digest a1b2c3d4e5f6."
    )


def test_approved_message_self_approved(clean_env):
    manifest = _manifest()
    manifest["approval"]["self_approved"] = True
    assert "self-approved: yes" in approval_notice_message(
        "p1", manifest, "plan_approved"
    )


def test_changes_requested_message_omits_reason(clean_env):
    msg = approval_notice_message("p1", _manifest(), "plan_changes_requested")
    assert msg == (
        "Changes were requested on plan p1 by alice@example.com, "
        "digest a1b2c3d4e5f6."
    )
    assert SENTINELS[6] not in msg


def test_warning_count_totals_every_story(clean_env, monkeypatch):
    from pipeline import plan_review

    monkeypatch.setattr(
        plan_review,
        "review_warnings",
        lambda manifest: {"S-1": ["w1", "w2"], "S-2": ["w3"], "S-3": []},
    )
    msg = approval_notice_message("p1", _manifest(), "plan_pending_approval")
    assert "3 warnings" in msg


@pytest.mark.parametrize("event", APPROVAL_EVENTS)
def test_messages_never_leak_story_or_config_data(clean_env, event):
    msg = approval_notice_message("p1", _manifest(), event)
    for sentinel in SENTINELS:
        assert sentinel not in msg


def test_unknown_event_raises_value_error(clean_env):
    with pytest.raises(ValueError, match="plan_deleted"):
        approval_notice_message("p1", _manifest(), "plan_deleted")


def test_link_default_path(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", "https://pipeline.example.com:8443")
    monkeypatch.delenv("PIPELINE_APPROVAL_LINK_PATH", raising=False)
    msg = approval_notice_message("p1", _manifest(), "plan_approved")
    assert msg.endswith(" Review: https://pipeline.example.com:8443/?plan=p1")


def test_link_custom_path(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", "https://pipeline.example.com:8443")
    monkeypatch.setenv("PIPELINE_APPROVAL_LINK_PATH", "/ext/approvals/review.html")
    msg = approval_notice_message("p1", _manifest(), "plan_approved")
    assert msg.endswith(
        " Review: https://pipeline.example.com:8443/ext/approvals/review.html?plan=p1"
    )


def test_link_quotes_plan_name(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", "https://h.example")
    monkeypatch.delenv("PIPELINE_APPROVAL_LINK_PATH", raising=False)
    msg = approval_notice_message("a b/c&d", _manifest(), "plan_approved")
    assert msg.endswith(" Review: https://h.example/?plan=a%20b%2Fc%26d")


@pytest.mark.parametrize("path", ["/a/../b", "//x", "x", "/a b"])
def test_invalid_link_path_yields_no_link_and_warns(monkeypatch, caplog, path):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", "https://h.example")
    monkeypatch.setenv("PIPELINE_APPROVAL_LINK_PATH", path)
    with caplog.at_level(logging.WARNING):
        msg = approval_notice_message("p1", _manifest(), "plan_approved")
    assert "Review:" not in msg
    assert "PIPELINE_APPROVAL_LINK_PATH" in caplog.text
    assert path not in caplog.text
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://h.example",
        "https://u:p@h.example",
        "https://h.example/?x=1",
        "https://h.example/#f",
        "https://h.example/sub",
    ],
)
def test_invalid_origin_yields_no_link_and_warns(monkeypatch, caplog, origin):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", origin)
    monkeypatch.delenv("PIPELINE_APPROVAL_LINK_PATH", raising=False)
    with caplog.at_level(logging.WARNING):
        msg = approval_notice_message("p1", _manifest(), "plan_approved")
    assert "Review:" not in msg
    assert "PIPELINE_DASHBOARD_PUBLIC_URL" in caplog.text
    assert origin not in caplog.text
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


@pytest.mark.parametrize("origin", ["", "   "])
def test_unset_origin_yields_no_link_without_warning(monkeypatch, caplog, origin):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", origin)
    with caplog.at_level(logging.WARNING):
        msg = approval_notice_message("p1", _manifest(), "plan_approved")
    assert "Review:" not in msg
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_notify_calls_notify_user_once(monkeypatch):
    calls = []

    def _stub(plan_name, message, **kwargs):
        calls.append((plan_name, message, kwargs))

    monkeypatch.setattr("pipeline.server._notify_user", _stub)
    monkeypatch.delenv("PIPELINE_DASHBOARD_PUBLIC_URL", raising=False)
    notify_approval_event("p1", _manifest(), "plan_approved")
    assert len(calls) == 1
    plan_name, message, kwargs = calls[0]
    assert plan_name == "p1"
    assert message == approval_notice_message("p1", _manifest(), "plan_approved")
    assert kwargs["event"] == "plan_approved"
    assert kwargs["dedup_key"] == "plan_approved:p1:a1b2c3d4e5f6"


def test_notify_never_raises(monkeypatch, caplog):
    def _boom(plan_name, message, **kwargs):
        raise RuntimeError("SENTINEL-NOTIFY-FAILURE")

    monkeypatch.setattr("pipeline.server._notify_user", _boom)
    monkeypatch.delenv("PIPELINE_DASHBOARD_PUBLIC_URL", raising=False)
    with caplog.at_level(logging.WARNING):
        notify_approval_event("p1", _manifest(), "plan_approved")
    assert "SENTINEL-NOTIFY-FAILURE" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize(
    ("event", "label"),
    [
        ("plan_pending_approval", "plan awaiting approval"),
        ("plan_approved", "plan approved"),
        ("plan_changes_requested", "plan changes requested"),
    ],
)
def test_subject_is_plan_level(event, label):
    rendered = render_notification(
        {"plan": "P", "story_key": "S-1", "payload": {"event": event, "message": "x"}}
    )
    assert rendered.subject == f"[pipeline] {label}: P"


def _write_config(tmp_path, data):
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_routing_matches_approval_events_for_plan_and_owner(tmp_path):
    from pipeline import notification_channels as nc

    class _FakeChannel:
        type_name = "fakepan02"

        def __init__(self, name):
            self.name = name
            self.outbox_suffix = f".outbox.{name}.jsonl"

        @classmethod
        def from_settings(cls, name, settings, env):
            return cls(name)

        def send(self, record):
            return nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)

    if "fakepan02" not in nc.registered_types():
        nc.register_channel_type(_FakeChannel)

    config = load_notification_config(
        _write_config(
            tmp_path,
            {
                "version": 1,
                "instances": {"lead": {"type": "fakepan02"}},
                "routes": [
                    {
                        "match": {"plan": "team-*", "owner": "alice*"},
                        "deliver": [
                            {
                                "events": [
                                    "plan_pending_approval",
                                    "plan_approved",
                                    "plan_changes_requested",
                                ],
                                "to": ["lead"],
                            }
                        ],
                    }
                ],
            },
        ),
        env={},
    )
    assert config.resolve("team-x", "plan_approved", "alice@x") == ["lead"]
    assert config.resolve("team-x", "plan_approved", "bob") == []
