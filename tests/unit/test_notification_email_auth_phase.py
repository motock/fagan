"""NTF-02: the email channel reports the login phase of an SMTP send.

Gmail rate-limits the login with ``454 4.7.0`` and then drops the connection on
smtplib's fallback AUTH LOGIN.  ``smtplib.login`` raises only the LAST error, so
without a dedicated wrapper the log and the ``DeliveryResult`` show a bare
``SMTPServerDisconnected`` that reads like a network fault.  A temporary (4xx)
authentication reply must also be a RETRY rather than a permanent rejection.

These tests stand up a fake ``smtplib.SMTP`` and drive the public channel
``send`` path, so they cover the class name that reaches the log as well as the
returned status/reason.
"""

from __future__ import annotations

import logging
import smtplib
from typing import ClassVar, Self

import pytest

from pipeline.notification_channels import DeliveryStatus
from pipeline.notification_email import EmailChannelType

PASSWORD = "fake-not-a-real-password"


class FakeSMTP:
    """Minimal stand-in for ``smtplib.SMTP``.

    ``login_error`` and ``send_error`` are class attributes so a test can arm a
    single failure without reaching into the channel's internals.
    """

    login_error: ClassVar[Exception | None] = None
    send_error: ClassVar[Exception | None] = None
    sent: ClassVar[list[object]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def starttls(self, context: object = None) -> None:
        self.starttls_context = context

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, message: object) -> None:
        if self.send_error is not None:
            raise self.send_error
        FakeSMTP.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_fake_smtp() -> None:
    """Drop armed errors (and sent mail) between tests.

    Without this, a login error left armed by one test would poison every later
    test with a stale disconnect.
    """
    FakeSMTP.login_error = None
    FakeSMTP.send_error = None
    FakeSMTP.sent = []


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> EmailChannelType:
    monkeypatch.setattr("pipeline.notification_email.smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("NTF_TEST_PW", PASSWORD)
    return EmailChannelType(
        "email",
        host="smtp.example.test",
        port=587,
        user="u@example.test",
        from_addr="u@example.test",
        to_addrs=("t@example.test",),
        timeout=5.0,
        password_env="NTF_TEST_PW",
    )


def _record() -> dict:
    return {"type": "notification", "plan": "p", "payload": {"message": "m"}}


def test_should_retry_when_login_connection_drops(channel: EmailChannelType) -> None:
    FakeSMTP.login_error = smtplib.SMTPServerDisconnected("connection reset by peer")

    result = channel.send(_record())

    assert result.status is DeliveryStatus.RETRY
    assert result.reason == "smtp_auth_disconnect"


def test_should_retry_on_temporary_auth_rejection(channel: EmailChannelType) -> None:
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(
        454, b"4.7.0 Too many login attempts, please try again later"
    )

    result = channel.send(_record())

    assert result.status is DeliveryStatus.RETRY
    assert result.reason == "smtp_auth_temporary"


def test_should_reject_on_permanent_auth_rejection(channel: EmailChannelType) -> None:
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted"
    )

    result = channel.send(_record())

    assert result.status is DeliveryStatus.REJECTED
    assert result.reason == "smtp_rejected"


@pytest.mark.parametrize(
    ("smtp_code", "expected_status", "expected_reason"),
    [
        (499, DeliveryStatus.RETRY, "smtp_auth_temporary"),
        (500, DeliveryStatus.REJECTED, "smtp_rejected"),
    ],
)
def test_should_split_on_the_4xx_boundary(
    channel: EmailChannelType,
    smtp_code: int,
    expected_status: DeliveryStatus,
    expected_reason: str,
) -> None:
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(smtp_code, b"boundary reply")

    result = channel.send(_record())

    assert result.status is expected_status
    assert result.reason == expected_reason


def test_should_not_report_auth_phase_when_send_phase_drops(
    channel: EmailChannelType,
) -> None:
    FakeSMTP.send_error = smtplib.SMTPServerDisconnected("connection reset by peer")

    result = channel.send(_record())

    assert result.status is DeliveryStatus.RETRY
    assert result.reason == ""
    assert result.reason != "smtp_auth_disconnect"


def test_should_log_auth_phase_type_without_the_password(
    channel: EmailChannelType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakeSMTP.login_error = smtplib.SMTPServerDisconnected("connection reset by peer")

    with caplog.at_level(logging.WARNING, logger="pipeline.notification_email"):
        channel.send(_record())

    messages = [record.getMessage() for record in caplog.records]
    assert any("error_type=SMTPAuthPhaseDisconnect" in message for message in messages)
    assert all(PASSWORD not in message for message in messages)


def test_should_deliver_when_login_and_send_succeed(channel: EmailChannelType) -> None:
    result = channel.send(_record())

    assert result.status is DeliveryStatus.DELIVERED
    assert len(FakeSMTP.sent) == 1
