"""Tests for the routable ``slack`` channel type (NC-07).

Only the HTTP opener boundary (``_build_opener``) is faked.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from email.message import Message

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_slack as ns

URL = "https://hooks.slack.test/services/T000/B000/SECRETTOKEN"
RECORD = {"plan": "PLAN-1", "payload": {"message": "hello world", "event": "plan_completed"}}


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
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return FakeResponse(self.outcome)


@pytest.fixture
def opener(monkeypatch):
    holder = {"opener": FakeOpener(200), "built": 0}

    def build():
        holder["built"] += 1
        return holder["opener"]

    monkeypatch.setattr(ns, "_build_opener", build)
    monkeypatch.setenv("TEAM_SLACK_HOOK", URL)
    return holder


def _channel(**overrides):
    settings = {"type": "slack", "webhook_url_env": "TEAM_SLACK_HOOK", **overrides}
    return ns.SlackChannelType.from_settings("team-slack", settings, {})


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid/", code, "x", headers, None)


def _send_with(opener, outcome, record=RECORD):
    opener["opener"] = FakeOpener(outcome)
    return _channel().send(record)


# ---- registration and settings ------------------------------------------------


def test_should_register_slack_type():
    assert nc.channel_type("slack") is ns.SlackChannelType


def test_should_default_timeout_to_20():
    assert _channel()._timeout == 20.0


def test_should_accept_custom_timeout():
    assert _channel(timeout=5)._timeout == 5.0


def test_should_name_outbox_per_instance():
    assert _channel().outbox_suffix == ".outbox.team-slack.jsonl"


@pytest.mark.parametrize("bad", [0, -1, "20", None, True, float("nan"), float("inf")])
def test_should_reject_bad_timeout(bad):
    with pytest.raises(nc.ChannelConfigError, match="timeout"):
        _channel(timeout=bad)


def test_should_reject_inline_webhook_url_without_echoing_it():
    with pytest.raises(nc.ChannelConfigError) as err:
        ns.SlackChannelType.from_settings("s", {"type": "slack", "webhook_url": URL}, {})
    assert "SECRETTOKEN" not in str(err.value)


def test_should_require_webhook_url_env():
    with pytest.raises(nc.ChannelConfigError, match="webhook_url_env"):
        ns.SlackChannelType.from_settings("s", {"type": "slack"}, {})


def test_should_reject_empty_webhook_url_env():
    with pytest.raises(nc.ChannelConfigError, match="webhook_url_env"):
        _channel(webhook_url_env="")


def test_should_reject_unknown_setting():
    with pytest.raises(nc.ChannelConfigError, match="unknown setting"):
        _channel(channel="#general")


# ---- delivery outcomes --------------------------------------------------------


def test_should_deliver_on_2xx(opener):
    assert _send_with(opener, 200).status is nc.DeliveryStatus.DELIVERED


def test_should_post_json_text_to_the_webhook_with_timeout(opener):
    _send_with(opener, 200)
    fake = opener["opener"]
    request = fake.requests[0]
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert "hello world" in json.loads(request.data)["text"]
    assert fake.timeouts == [20.0]


def test_should_retry_on_429_with_retry_after(opener):
    result = _send_with(opener, _http_error(429, "30"))
    assert result.status is nc.DeliveryStatus.RETRY
    assert result.retry_after_seconds == 30.0


@pytest.mark.parametrize("header", ["-5", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "nan", "inf"])
def test_should_ignore_invalid_retry_after(opener, header):
    result = _send_with(opener, _http_error(429, header))
    assert result.status is nc.DeliveryStatus.RETRY
    assert result.retry_after_seconds is None


def test_should_retry_on_429_without_retry_after(opener):
    result = _send_with(opener, _http_error(429))
    assert result.status is nc.DeliveryStatus.RETRY
    assert result.retry_after_seconds is None


@pytest.mark.parametrize("code", [500, 502, 503])
def test_should_retry_on_5xx(opener, code):
    assert _send_with(opener, _http_error(code)).status is nc.DeliveryStatus.RETRY


@pytest.mark.parametrize("code", [400, 403, 404, 410])
def test_should_reject_other_4xx(opener, code):
    result = _send_with(opener, _http_error(code))
    assert result.status is nc.DeliveryStatus.REJECTED
    assert result.reason == "http_4xx"


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        socket.gaierror("dns"),
        ConnectionRefusedError("refused"),
        urllib.error.URLError("unreachable"),
    ],
)
def test_should_retry_on_network_errors(opener, error):
    assert _send_with(opener, error).status is nc.DeliveryStatus.RETRY


def test_should_retry_on_unexpected_exception_without_raising(opener):
    assert _send_with(opener, RuntimeError(URL)).status is nc.DeliveryStatus.RETRY


# ---- misconfiguration ---------------------------------------------------------


def test_should_retry_misconfigured_when_env_unset(opener, monkeypatch, caplog):
    monkeypatch.delenv("TEAM_SLACK_HOOK")
    with caplog.at_level(logging.DEBUG):
        result = _channel().send(RECORD)
    assert (result.status, result.reason) == (nc.DeliveryStatus.RETRY, "misconfigured")
    assert opener["built"] == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert "TEAM_SLACK_HOOK" in errors[0].getMessage()


@pytest.mark.parametrize(
    "bad_url",
    ["http://hooks.slack.test/x", "ftp://hooks.slack.test/x", "HTTP://hooks.slack.test/x",
     "not a url", "https://", "https://[bad", "https://host/a b", ""],
)
def test_should_retry_misconfigured_for_bad_url(opener, monkeypatch, bad_url):
    monkeypatch.setenv("TEAM_SLACK_HOOK", bad_url)
    result = _channel().send(RECORD)
    assert (result.status, result.reason) == (nc.DeliveryStatus.RETRY, "misconfigured")


def test_should_never_open_a_connection_for_http_url(opener, monkeypatch):
    monkeypatch.setenv("TEAM_SLACK_HOOK", "http://hooks.slack.test/x")
    _channel().send(RECORD)
    assert opener["built"] == 0
    assert opener["opener"].requests == []


def test_should_recover_once_env_is_fixed(opener, monkeypatch):
    monkeypatch.delenv("TEAM_SLACK_HOOK")
    channel = _channel()
    assert channel.send(RECORD).status is nc.DeliveryStatus.RETRY
    monkeypatch.setenv("TEAM_SLACK_HOOK", URL)
    assert channel.send(RECORD).status is nc.DeliveryStatus.DELIVERED


# ---- redirects ----------------------------------------------------------------


def test_should_raise_instead_of_following_a_redirect():
    handler = ns._NoRedirectHandler()
    request = urllib.request.Request(URL, data=b"{}", method="POST")
    with pytest.raises(ns._RedirectBlocked):
        handler.redirect_request(request, None, 302, "Found", Message(), "https://evil.test/")


def test_should_build_opener_with_redirect_blocking_handler():
    opener = ns._build_opener()
    assert any(isinstance(h, ns._NoRedirectHandler) for h in opener.handlers)


def test_should_build_opener_with_verifying_tls_context():
    import ssl

    opener = ns._build_opener()
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https[0]._context.verify_mode == ssl.CERT_REQUIRED
    assert https[0]._context.check_hostname is True


def test_should_retry_when_redirect_is_blocked(opener):
    result = _send_with(opener, ns._RedirectBlocked())
    assert result.status is nc.DeliveryStatus.RETRY
    assert len(opener["opener"].requests) == 1


# ---- text ---------------------------------------------------------------------


def _sent_text(opener, message):
    _send_with(opener, 200, {"plan": "P", "payload": {"message": message, "event": "plan_completed"}})
    return json.loads(opener["opener"].requests[0].data)["text"]


def test_should_escape_slack_control_characters(opener):
    text = _sent_text(opener, "a & b <!channel> c")
    assert "a &amp; b &lt;!channel&gt; c" in text
    assert "<!channel>" not in text


def test_should_not_truncate_text_at_the_cap(opener):
    prefix = "[pipeline] plan complete: P\n"
    text = _sent_text(opener, "x" * (ns.MAX_TEXT_CHARS - len(prefix)))
    assert ns.TRUNCATION_MARKER not in text
    assert len(text) <= ns.MAX_TEXT_CHARS


def test_should_truncate_long_text_with_visible_marker(opener):
    text = _sent_text(opener, "x" * 10000)
    assert len(text) <= ns.MAX_TEXT_CHARS
    assert text.endswith(ns.TRUNCATION_MARKER)


def test_should_not_cut_an_escape_sequence_in_half(opener):
    text = _sent_text(opener, "&" * 10000)
    body = text[: -len(ns.TRUNCATION_MARKER)].replace("&amp;", "").rstrip("\n")
    assert len(text) <= ns.MAX_TEXT_CHARS
    assert "&" not in body.split("\n", 1)[-1]


# ---- malformed record ---------------------------------------------------------


@pytest.mark.parametrize("record", [{"plan": "P", "payload": {"message": 123}}, {"payload": "x"}, None])
def test_should_reject_malformed_record(opener, record):
    result = _channel().send(record)
    assert (result.status, result.reason) == (nc.DeliveryStatus.REJECTED, "malformed_record")
    assert opener["built"] == 0


# ---- secrecy ------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [200, _http_error(403), _http_error(429, "5"), _http_error(500),
     TimeoutError(URL), urllib.error.URLError(URL), RuntimeError(URL)],
    ids=["ok", "4xx", "429", "5xx", "timeout", "urlerror", "other"],
)
def test_should_never_leak_url_or_payload_in_logs_or_result(opener, caplog, outcome):
    with caplog.at_level(logging.DEBUG):
        result = _send_with(opener, outcome)
    assert "SECRETTOKEN" not in repr(result)
    for rec in caplog.records:
        rendered = rec.getMessage() + repr(rec.__dict__.get("args")) + str(rec.exc_info)
        assert "SECRETTOKEN" not in rendered
        assert "hooks.slack.test" not in rendered
        assert "hello world" not in rendered


def test_should_log_exception_type_only_on_failure(opener, caplog):
    with caplog.at_level(logging.DEBUG):
        _send_with(opener, urllib.error.URLError(URL))
    assert any("URLError" in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


def test_should_not_log_payload_at_info_or_below_on_success(opener, caplog):
    with caplog.at_level(logging.DEBUG):
        _send_with(opener, 200)
    assert all("hello world" not in r.getMessage() for r in caplog.records)


def test_should_not_leak_url_when_env_value_is_bad(opener, monkeypatch, caplog):
    monkeypatch.setenv("TEAM_SLACK_HOOK", "http://hooks.slack.test/SECRETTOKEN")
    with caplog.at_level(logging.DEBUG):
        result = _channel().send(RECORD)
    assert "SECRETTOKEN" not in repr(result)
    assert all("SECRETTOKEN" not in r.getMessage() for r in caplog.records)


def test_should_never_raise_even_if_opener_build_fails(monkeypatch):
    monkeypatch.setenv("TEAM_SLACK_HOOK", URL)

    def boom():
        raise OSError(URL)

    monkeypatch.setattr(ns, "_build_opener", boom)
    assert _channel().send(RECORD).status is nc.DeliveryStatus.RETRY


def test_should_reject_huge_integer_timeout():
    with pytest.raises(nc.ChannelConfigError, match="timeout"):
        _channel(timeout=10**400)
