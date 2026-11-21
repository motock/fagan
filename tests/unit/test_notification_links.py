"""Story links in story-level notifications (NAU-02)."""

import logging

import pytest

from pipeline.notification_email import _build_message
from pipeline.notification_links import story_link
from pipeline.notification_render import render_notification
from pipeline.notification_slack import _with_link

ORIGIN = "https://h.example"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "PIPELINE_DASHBOARD_PUBLIC_URL",
        "PIPELINE_STORY_LINK_PATH",
        "PIPELINE_APPROVAL_LINK_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _record(event, story_key="s1"):
    return {
        "plan": "p1",
        "story_key": story_key,
        "payload": {"event": event, "message": "msg"},
    }


def test_story_link_quotes_plan_and_story(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("PIPELINE_STORY_LINK_PATH", "/overlord")
    assert story_link("p/1", "s 1") == "https://h.example/overlord?plan=p%2F1&story=s%201"


def test_story_link_default_path_is_root(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    assert story_link("p", "s") == "https://h.example/?plan=p&story=s"


def test_story_link_none_when_origin_unset(caplog):
    with caplog.at_level(logging.WARNING):
        assert story_link("p", "s") is None
    assert not caplog.records


@pytest.mark.parametrize(
    "origin",
    [
        "http://h.example",
        "https://h.example/sub",
        "https://user:pw-SECRET@h.example",
        "https://h.example?q=1",
        "https://h.example#frag",
    ],
)
def test_story_link_none_for_invalid_origin_logs_name_only(monkeypatch, caplog, origin):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", origin)
    with caplog.at_level(logging.WARNING):
        assert story_link("p", "s") is None
    assert "PIPELINE_DASHBOARD_PUBLIC_URL" in caplog.text
    assert origin not in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("path", ["/a/../b", "/a//b", "/a?x=1"])
def test_story_link_none_for_invalid_path(monkeypatch, caplog, path):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("PIPELINE_STORY_LINK_PATH", path)
    with caplog.at_level(logging.WARNING):
        assert story_link("p", "s") is None
    assert "PIPELINE_STORY_LINK_PATH" in caplog.text
    assert path not in caplog.text


def test_story_parked_record_carries_link(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    assert render_notification(_record("story_parked")).link == (
        "https://h.example/?plan=p1&story=s1"
    )


def test_plan_completed_record_has_no_link(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    assert render_notification(_record("plan_completed")).link is None


def test_story_level_record_without_story_key_has_no_link(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    assert render_notification(_record("story_parked", story_key=None)).link is None


def test_plan_pending_approval_message_unchanged(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    rendered = render_notification(_record("plan_pending_approval"))
    assert rendered.body == "msg"
    assert rendered.link is None


def test_slack_body_ends_with_escaped_open_line(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    rendered = render_notification(_record("story_parked", story_key="a&b"))
    assert _with_link(rendered).endswith("\nOpen: https://h.example/?plan=p1&story=a%26b")


def test_slack_body_unchanged_without_link():
    assert _with_link(render_notification(_record("story_parked"))) == "msg"


class _Settings:
    from_addr = "a@example.com"
    to_addrs = ("b@example.com",)


def test_email_body_ends_with_open_line(monkeypatch):
    monkeypatch.setenv("PIPELINE_DASHBOARD_PUBLIC_URL", ORIGIN)
    message = _build_message(_Settings(), render_notification(_record("story_parked")))
    assert message.get_content().rstrip("\n").endswith(
        "Open: https://h.example/?plan=p1&story=s1"
    )


def test_email_body_unchanged_without_link():
    message = _build_message(_Settings(), render_notification(_record("story_parked")))
    assert message.get_content().strip() == "msg"
