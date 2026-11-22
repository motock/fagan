"""Notification channels resolve their credentials in the bound namespace.

The outbox drain delivers a namespace's notices inside ``bind_namespace``.
Each channel builds its ``CredentialRef`` at send time with
``current_namespace()``, so a credentials plugin can tell which team a notice
belongs to. Only ``resolve`` (captured at the channel module boundary) and the
network boundary are faked.
"""

from __future__ import annotations

import smtplib
import socket

import httpx
import pytest

from app.credentials import SecretValue
from pipeline import notification_channels as nc
from pipeline import notification_email as ne
from pipeline import notification_slack as ns
from pipeline import notification_webhook as nw
from pipeline.actor import bind_namespace

NAMESPACE = "acme/core"

SMTP_SECRET = "made-up-smtp-secret-7c1e"
SLACK_URL = "https://hooks.slack.test/services/T000/B000/SLACKTOKEN"
WEBHOOK_URL = "https://hooks.example.test/inbound/WEBHOOKTOKEN"
WEBHOOK_KEY = "made-up-webhook-key-4d2b"

RECORD = {
    "plan": "PLAN-1",
    "story_key": "s-1",
    "payload": {"message": "hello world", "event": "plan_completed"},
}


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def open(self, request, timeout=None):
        return FakeResponse(200)


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        pass


@pytest.fixture
def resolved(monkeypatch):
    """Capture every CredentialRef each channel module resolves."""
    refs = []
    values = {
        "tenant.smtp": SMTP_SECRET,
        "tenant.slack": SLACK_URL,
        "hook.url": WEBHOOK_URL,
        "hook.key": WEBHOOK_KEY,
    }

    def fake_resolve(ref):
        refs.append(ref)
        return SecretValue(values[ref.name])

    for module in (ne, ns, nw):
        monkeypatch.setattr(module, "resolve", fake_resolve)
    return refs


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(ns, "_build_opener", lambda: FakeOpener())
    monkeypatch.setattr(
        nw,
        "_client",
        lambda url, address, timeout: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            follow_redirects=False,
        ),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


def _email(**overrides):
    settings = {
        "type": "email",
        "host": "smtp.example.test",
        "port": 587,
        "user": "notifier",
        "from": "pipeline@example.test",
        "to": ["team@example.test"],
        "password_ref": "tenant.smtp",
        **overrides,
    }
    return ne.EmailChannelType.from_settings("ns-mail", settings, {})


def _slack(**overrides):
    settings = {"type": "slack", "webhook_url_ref": "tenant.slack", **overrides}
    return ns.SlackChannelType.from_settings("ns-slack", settings, {})


def _webhook(**overrides):
    settings = {
        "type": "webhook",
        "url_ref": "hook.url",
        "secret_ref": "hook.key",
        **overrides,
    }
    return nw.WebhookChannelType.from_settings("ns-hook", settings, {})


def test_should_resolve_the_email_password_in_the_bound_namespace(resolved, network):
    with bind_namespace(NAMESPACE):
        result = _email().send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert len(resolved) == 1
    assert resolved[0].namespace == NAMESPACE
    assert (resolved[0].purpose, resolved[0].name) == ("notification", "tenant.smtp")


def test_should_resolve_the_slack_webhook_in_the_bound_namespace(resolved, network):
    with bind_namespace(NAMESPACE):
        result = _slack().send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert len(resolved) == 1
    assert resolved[0].namespace == NAMESPACE
    assert (resolved[0].purpose, resolved[0].name) == ("notification", "tenant.slack")


def test_should_resolve_both_webhook_refs_in_the_bound_namespace(resolved, network):
    with bind_namespace(NAMESPACE):
        result = _webhook().send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert [(r.purpose, r.name, r.namespace) for r in resolved] == [
        ("notification", "hook.url", NAMESPACE),
        ("notification", "hook.key", NAMESPACE),
    ]


@pytest.mark.parametrize(
    "build",
    [_email, _slack, _webhook],
    ids=["email", "slack", "webhook"],
)
def test_should_use_the_default_namespace_when_nothing_is_bound(build, resolved, network):
    build().send(RECORD)

    assert resolved
    assert {r.namespace for r in resolved} == {"default"}


def test_should_read_the_namespace_at_send_time(resolved, network):
    channel = _email()

    with bind_namespace(NAMESPACE):
        channel.send(RECORD)

    assert [r.namespace for r in resolved] == [NAMESPACE]


def test_should_not_read_the_store_or_actor_when_only_validating(resolved):
    _email()
    _slack()
    _webhook()

    assert resolved == []
