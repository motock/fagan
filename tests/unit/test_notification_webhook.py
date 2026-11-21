"""Tests for the signed ``webhook`` channel type.

Only the HTTP opener, ``socket.getaddrinfo`` and the credential seam's
``resolve`` are faked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
from email.message import Message

import pytest

from app.credentials import SecretValue
from pipeline import notification_channels as nc
from pipeline import notification_webhook as nw

URL = "https://hooks.example.test/in/SECRETPATH"
KEY = "s3cr3t-hmac-key"
RECORD = {
    "plan": "PLAN-1",
    "story_key": "s-1",
    "payload": {"message": "hello world", "event": "story_parked"},
}


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return FakeResponse(self.outcome)


@pytest.fixture
def env(monkeypatch):
    state = {
        "opener": FakeOpener(200),
        "url": URL,
        "addrs": ["93.184.216.34"],
    }
    monkeypatch.setattr(nw, "_build_opener", lambda: state["opener"])
    monkeypatch.setattr(
        nw,
        "resolve",
        lambda ref: SecretValue(state["url"] if ref.name == "hook.url" else KEY),
    )
    monkeypatch.setattr(
        nw.socket,
        "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", (a, port)) for a in state["addrs"]],
    )
    return state


def _channel(**overrides):
    settings = {"type": "webhook", "url_ref": "hook.url", "secret_ref": "hook.key", **overrides}
    return nw.WebhookChannelType.from_settings("my-hook", settings, {})


def _send(env, outcome=200, record=RECORD):
    env["opener"] = FakeOpener(outcome)
    return _channel().send(record)


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x.invalid/", code, "x", headers, None)


def test_should_register_webhook_type():
    assert nc.channel_type("webhook") is nw.WebhookChannelType


def test_should_post_exact_body_with_verifiable_signature(env):
    result = _send(env)
    assert result.status is nc.DeliveryStatus.DELIVERED
    (req,) = env["opener"].requests
    body = req.data.decode()
    payload = json.loads(body)
    assert body == json.dumps(payload, separators=(",", ":"), sort_keys=True)
    assert payload["version"] == 1
    assert payload["event"] == "story_parked"
    assert payload["plan"] == "PLAN-1"
    assert payload["story_key"] == "s-1"
    assert payload["body"] == "hello world"
    assert set(payload) == {
        "version", "event", "plan", "story_key", "subject", "body", "link", "sent_at",
    }
    ts = req.get_header("X-fagan-timestamp")
    expected = hmac.new(KEY.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    assert req.get_header("X-fagan-signature") == f"sha256={expected}"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("User-agent") == "fagan-notify/1"
    assert req.get_method() == "POST"


def test_should_deliver_on_200(env):
    assert _send(env, 200).status is nc.DeliveryStatus.DELIVERED


def test_should_retry_429_honouring_retry_after(env):
    result = _send(env, _http_error(429, "7"))
    assert result.status is nc.DeliveryStatus.RETRY
    assert result.retry_after_seconds == 7.0


def test_should_retry_500(env):
    assert _send(env, _http_error(500)).status is nc.DeliveryStatus.RETRY


def test_should_retry_408(env):
    assert _send(env, _http_error(408)).status is nc.DeliveryStatus.RETRY


def test_should_reject_404(env):
    assert _send(env, _http_error(404)).status is nc.DeliveryStatus.REJECTED


def test_should_reject_redirect(env):
    assert _send(env, nw._RedirectBlocked()).status is nc.DeliveryStatus.REJECTED


def test_should_retry_connection_error(env):
    assert _send(env, OSError("boom")).status is nc.DeliveryStatus.RETRY


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/x",
        "https://user:pw@hooks.example.test/x",
        "https://hooks.example.test/x#frag",
        "https://93.184.216.34/x",
        "https://[::1]/x",
        "https://localhost/x",
        "https://LOCALHOST./x",
    ],
)
def test_should_reject_disallowed_url_without_request(env, url):
    env["url"] = url
    result = _send(env)
    assert result.status is nc.DeliveryStatus.REJECTED
    assert env["opener"].requests == []


@pytest.mark.parametrize("addr", ["10.0.0.5", "127.0.0.1", "169.254.1.1", "::1"])
def test_should_reject_host_resolving_to_non_public_address(env, addr):
    env["addrs"] = [addr]
    assert _send(env).status is nc.DeliveryStatus.REJECTED
    assert env["opener"].requests == []


def test_should_reject_when_any_resolved_address_is_private(env):
    env["addrs"] = ["93.184.216.34", "10.0.0.5"]
    assert _send(env).status is nc.DeliveryStatus.REJECTED
    assert env["opener"].requests == []


@pytest.mark.parametrize(
    "settings",
    [
        {"type": "webhook", "secret_ref": "hook.key"},
        {"type": "webhook", "url_ref": "hook.url"},
        {"type": "webhook", "url_ref": "hook.url", "secret_ref": "hook.key", "url_env": "LEAKY_VAR"},
        {"type": "webhook", "url_ref": "hook.url", "secret_ref": "hook.key", "bogus": "LEAKY_VAR"},
        {"type": "webhook", "url_ref": "hook.url", "secret_ref": "hook.key", "timeout": 0},
        {"type": "webhook", "url_ref": "hook.url", "secret_ref": "hook.key", "timeout": 31},
    ],
)
def test_should_refuse_invalid_settings_without_echoing_values(settings):
    with pytest.raises(nc.ChannelConfigError) as err:
        nw.WebhookChannelType.from_settings("my-hook", settings, {})
    assert "LEAKY_VAR" not in str(err.value)
    assert "hook.url" not in str(err.value)


def test_should_accept_timeout_bounds():
    _channel(timeout=1)
    _channel(timeout=30)


def test_should_reject_unresolvable_reference_and_log_no_value(env, monkeypatch, caplog):
    def boom(ref):
        raise RuntimeError(f"cannot resolve {ref.name}")

    monkeypatch.setattr(nw, "resolve", boom)
    with caplog.at_level(logging.DEBUG):
        result = _channel().send(RECORD)
    assert result.status is nc.DeliveryStatus.REJECTED
    assert env["opener"].requests == []
    assert "hook.url" not in caplog.text
    assert "hook.key" not in caplog.text


def test_should_not_log_url_secret_or_body(env, caplog):
    env["url"] = "http://hooks.example.test/SECRETPATH"
    with caplog.at_level(logging.DEBUG):
        _channel().send(RECORD)
    assert "SECRETPATH" not in caplog.text
    assert "hello world" not in caplog.text
    assert KEY not in caplog.text
