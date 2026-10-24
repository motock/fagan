"""Tests for the routable ``email`` channel type (NC-06).

Only ``smtplib.SMTP`` is faked.
"""

from __future__ import annotations

import logging
import smtplib
from typing import ClassVar

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_email as ne

PASSWORD = "s3cret-pw-value"
RECORD = {"plan": "PLAN-1", "payload": {"message": "hello world", "event": "plan_completed"}}


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    ctor_error: Exception | None = None
    send_error: Exception | None = None
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.ctor_error:
            raise FakeSMTP.ctor_error
        self.host, self.port, self.timeout = host, port, timeout
        self.login_args = None
        self.sent = []
        self.tls_context = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls_context = context

    def login(self, user, password):
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.login_args = (user, password)

    def send_message(self, message):
        if FakeSMTP.send_error:
            raise FakeSMTP.send_error
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.ctor_error = FakeSMTP.send_error = FakeSMTP.login_error = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("TEAM_SMTP_PW", PASSWORD)
    return FakeSMTP


def _settings(**overrides):
    base = {
        "type": "email",
        "host": "smtp.example.test",
        "port": 587,
        "user": "notifier",
        "from": "pipeline@example.test",
        "to": ["team@example.test"],
        "password_env": "TEAM_SMTP_PW",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _channel(**overrides):
    return ne.EmailChannelType.from_settings("team-mail", _settings(**overrides), {})


def test_should_register_email_type():
    assert nc.channel_type("email") is ne.EmailChannelType


def test_should_build_instance_with_outbox_suffix():
    ch = _channel()
    assert ch.name == "team-mail"
    assert ch.outbox_suffix == ".outbox.team-mail.jsonl"


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": None},
        {"host": ""},
        {"to": None},
        {"to": []},
        {"to": "team@example.test"},
        {"to": ["team@example.test\r\nBcc: evil@example.test"]},
        {"to": ["a@b@example.test"]},
        {"to": ["no-at-sign"]},
        {"to": [42]},
        {"from": "bad\n@example.test"},
        {"from": None},
        {"port": 0},
        {"port": "587"},
        {"timeout": -1},
        {"password_env": ""},
        {"bogus": 1},
    ],
)
def test_should_reject_invalid_settings(overrides):
    with pytest.raises(nc.ChannelConfigError):
        _channel(**overrides)


def test_should_reject_inline_password():
    with pytest.raises(nc.ChannelConfigError, match="password"):
        _channel(password=PASSWORD)


def test_inline_password_error_does_not_echo_secret():
    with pytest.raises(nc.ChannelConfigError) as info:
        _channel(password=PASSWORD)
    assert PASSWORD not in str(info.value)


def test_should_not_store_password_on_instance():
    ch = _channel()
    assert PASSWORD not in repr(vars(ch))


def test_should_deliver_and_read_password_from_named_env(fake_smtp):
    result = _channel().send(RECORD)
    assert result.status is nc.DeliveryStatus.DELIVERED
    assert fake_smtp.instances[0].login_args == ("notifier", PASSWORD)


def test_should_read_password_at_send_time(monkeypatch, fake_smtp):
    ch = _channel()
    monkeypatch.setenv("TEAM_SMTP_PW", "rotated")
    ch.send(RECORD)
    assert fake_smtp.instances[0].login_args == ("notifier", "rotated")


def test_should_send_to_all_recipients(fake_smtp):
    _channel(to=["a@example.test", "b@example.test"]).send(RECORD)
    message = fake_smtp.instances[0].sent[0]
    assert message["To"] == "a@example.test, b@example.test"


def test_should_use_verifying_tls_context(fake_smtp):
    _channel().send(RECORD)
    ctx = fake_smtp.instances[0].tls_context
    assert ctx is not None and ctx.check_hostname is True


def test_should_pass_host_port_and_timeout(fake_smtp):
    _channel(port=2525, timeout=7).send(RECORD)
    inst = fake_smtp.instances[0]
    assert (inst.host, inst.port, inst.timeout) == ("smtp.example.test", 2525, 7.0)


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPResponseException(451, b"try later"),
        smtplib.SMTPServerDisconnected("gone"),
        ConnectionRefusedError("refused"),
        TimeoutError("slow"),
    ],
)
def test_should_retry_on_transient_errors(fake_smtp, error):
    fake_smtp.send_error = error
    assert _channel().send(RECORD).status is nc.DeliveryStatus.RETRY


def test_should_retry_when_connection_cannot_open(fake_smtp):
    fake_smtp.ctor_error = OSError("unreachable")
    assert _channel().send(RECORD).status is nc.DeliveryStatus.RETRY


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPRecipientsRefused({"a@example.test": (550, b"no such user")}),
        smtplib.SMTPResponseException(553, b"mailbox name invalid"),
    ],
)
def test_should_reject_on_permanent_recipient_error(fake_smtp, error):
    fake_smtp.send_error = error
    result = _channel().send(RECORD)
    assert result.status is nc.DeliveryStatus.REJECTED
    assert result.reason == "smtp_rejected"


def test_should_reject_on_auth_failure(fake_smtp):
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad creds")
    result = _channel().send(RECORD)
    assert result.status is nc.DeliveryStatus.REJECTED
    assert result.reason == "smtp_rejected"


@pytest.mark.parametrize(
    "record",
    [
        {"plan": "PLAN-1", "payload": {"message": {"body": "x"}}},
        {"plan": "PLAN-1", "payload": "not-a-mapping"},
        {"plan": "bad\r\nplan", "payload": {"message": "m"}},
    ],
)
def test_should_reject_malformed_record_without_connecting(fake_smtp, record):
    result = _channel().send(record)
    assert result.status is nc.DeliveryStatus.REJECTED
    assert result.reason == "malformed_record"
    assert fake_smtp.instances == []


def test_should_not_raise_on_non_dict_record():
    assert _channel().send(None).status is nc.DeliveryStatus.REJECTED


def test_password_absent_from_every_log_record(caplog, fake_smtp):
    fake_smtp.send_error = smtplib.SMTPResponseException(451, PASSWORD.encode())
    ch = _channel()
    with caplog.at_level(logging.DEBUG):
        ch.send(RECORD)
        fake_smtp.send_error = None
        ch.send(RECORD)
        ch.send({"plan": "P", "payload": {"message": {"body": PASSWORD}}})
    assert caplog.records
    for rec in caplog.records:
        assert PASSWORD not in rec.getMessage()
        assert PASSWORD not in str(rec.exc_info or "")


def test_recipients_absent_from_log_records(caplog, fake_smtp):
    fake_smtp.send_error = OSError("boom")
    with caplog.at_level(logging.DEBUG):
        _channel().send(RECORD)
    for rec in caplog.records:
        assert "team@example.test" not in rec.getMessage()


def test_legacy_sender_still_shares_core(monkeypatch, fake_smtp):
    for name, value in {
        "PIPELINE_NOTIFY_EMAIL_ENABLED": "1",
        "PIPELINE_NOTIFY_EMAIL_HOST": "h.example.test",
        "PIPELINE_NOTIFY_EMAIL_TO": "legacy@example.test",
        "PIPELINE_NOTIFY_EMAIL_USER": "u",
        "PIPELINE_NOTIFY_EMAIL_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(name, value)
    assert ne.send_notification_email(RECORD) is True
    assert fake_smtp.instances[0].sent[0]["To"] == "legacy@example.test"
