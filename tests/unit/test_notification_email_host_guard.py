"""Tests for the SMTP host guard in ``pipeline.notification_email``.

An org admin of a hosted tenant can save any SMTP host, so a notification
send must not reach an internal address unless the operator opted that exact
host in with ``PIPELINE_NOTIFY_SMTP_ALLOW_PRIVATE_HOSTS``.

Only two external boundaries are faked: ``socket.getaddrinfo`` (DNS) and
``smtplib.SMTP`` (the connection). No test opens a real socket. The one
socket-shaped object used is a ``socket.socketpair`` that never speaks TLS.
"""

from __future__ import annotations

import logging
import smtplib
import socket
from typing import ClassVar

import pytest

from app import outbound_guard
from pipeline import notification_email as ne
from pipeline.notification_channels import DeliveryStatus

ALLOW = "PIPELINE_NOTIFY_SMTP_ALLOW_PRIVATE_HOSTS"
ENABLED = "PIPELINE_NOTIFY_EMAIL_ENABLED"
HOST = "PIPELINE_NOTIFY_EMAIL_HOST"
TO = "PIPELINE_NOTIFY_EMAIL_TO"
FROM = "PIPELINE_NOTIFY_EMAIL_FROM"
USER = "PIPELINE_NOTIFY_EMAIL_USER"
PASSWORD = "PIPELINE_NOTIFY_EMAIL_PASSWORD"

PUBLIC_NAME = "smtp.public.example"
PUBLIC_ADDRESS = "93.184.216.34"
PUBLIC_ADDRESS_TWO = "93.184.216.35"
PRIVATE_NAME = "mail.corp.example"
PRIVATE_ADDRESS = "10.0.0.5"
REFUSED_TOKEN = "smtp_host_not_public"

RECORD = {"plan": "PLAN-42", "message": "Plan finished; 12 stories merged."}


class _Resolver:
    """Stub DNS. Unknown names fail to resolve, as a real lookup would."""

    def __init__(self, monkeypatch):
        self.answers: dict[str, list[str]] = {}
        self.lookups: list[str] = []
        monkeypatch.setattr(outbound_guard.socket, "getaddrinfo", self._getaddrinfo)

    def _getaddrinfo(self, host, port, *args, **kwargs):
        self.lookups.append(host)
        addresses = self.answers.get(host)
        if addresses is None:
            raise socket.gaierror(socket.EAI_NONAME, "name does not resolve")
        return [_info(address) for address in addresses]


def _info(address):
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))


class _FakeSMTP:
    """Recording stand-in for ``smtplib.SMTP``; never touches a socket."""

    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.starttls_context = None
        self.messages: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self, *, context=None):
        self.starttls_context = context

    def login(self, user, password):
        return None

    def send_message(self, message):
        self.messages.append(message)


@pytest.fixture
def resolver(monkeypatch):
    for name in (ENABLED, HOST, TO, FROM, USER, PASSWORD, ALLOW):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ENABLED, "1")
    monkeypatch.setenv(TO, "sink-recipient@example.test")
    monkeypatch.setenv(FROM, "pipeline@example.test")
    monkeypatch.setenv(PASSWORD, "pw-sentinel-value")
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(ne, "SMTP", _FakeSMTP, raising=False)
    return _Resolver(monkeypatch)


def _channel(host: str = PUBLIC_NAME) -> ne.EmailChannelType:
    return ne.EmailChannelType.from_settings(
        "notify",
        {
            "type": "email",
            "host": host,
            "from": "pipeline@example.test",
            "to": ["sink-recipient@example.test"],
            "password_env": PASSWORD,
        },
        {},
    )


def _only_connection() -> _FakeSMTP:
    assert len(_FakeSMTP.instances) == 1, "expected exactly one SMTP connection"
    return _FakeSMTP.instances[0]


def _tls_server_name(context) -> str | None:
    """The name the TLS layer would verify the certificate against."""
    left, right = socket.socketpair()
    try:
        with context.wrap_socket(left, do_handshake_on_connect=False) as tls:
            return tls.server_hostname
    finally:
        right.close()


def _all_log_text(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Positive paths
# ---------------------------------------------------------------------------


def test_public_name_connects_to_pinned_address_with_name_for_tls(resolver):
    resolver.answers[PUBLIC_NAME] = [PUBLIC_ADDRESS]

    result = _channel().send(RECORD)

    assert result.status is DeliveryStatus.DELIVERED
    smtp = _only_connection()
    assert smtp.args[0] == PUBLIC_ADDRESS
    assert _tls_server_name(smtp.starttls_context) == PUBLIC_NAME


def test_literal_public_address_connects_without_dns(resolver):
    result = _channel(PUBLIC_ADDRESS).send(RECORD)

    assert result.status is DeliveryStatus.DELIVERED
    assert _only_connection().args[0] == PUBLIC_ADDRESS
    assert resolver.lookups == []


def test_listed_host_resolving_internally_connects_to_pinned_address(
    resolver, monkeypatch
):
    monkeypatch.setenv(ALLOW, PRIVATE_NAME)
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    result = _channel(PRIVATE_NAME).send(RECORD)

    assert result.status is DeliveryStatus.DELIVERED
    smtp = _only_connection()
    assert smtp.args[0] == PRIVATE_ADDRESS
    assert _tls_server_name(smtp.starttls_context) == PRIVATE_NAME


def test_allow_list_is_case_folded_and_trailing_dot_stripped(resolver, monkeypatch):
    monkeypatch.setenv(ALLOW, " Mail.Corp.Example. ")
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    result = _channel("MAIL.corp.example.").send(RECORD)

    assert result.status is DeliveryStatus.DELIVERED
    assert _only_connection().args[0] == PRIVATE_ADDRESS


def test_legacy_sender_connects_to_pinned_public_address(resolver, monkeypatch):
    monkeypatch.setenv(HOST, PUBLIC_NAME)
    resolver.answers[PUBLIC_NAME] = [PUBLIC_ADDRESS]

    assert ne.send_notification_email(RECORD) is True

    smtp = _only_connection()
    assert smtp.args[0] == PUBLIC_ADDRESS
    assert _tls_server_name(smtp.starttls_context) == PUBLIC_NAME


# ---------------------------------------------------------------------------
# Negative paths: refusals open no connection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "::ffff:127.0.0.1",
    ],
)
def test_name_resolving_internally_is_refused_without_connecting(resolver, address):
    resolver.answers[PUBLIC_NAME] = [address]

    result = _channel().send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert result.reason == REFUSED_TOKEN
    assert _FakeSMTP.instances == []


def test_mixed_public_and_private_answers_are_refused(resolver):
    resolver.answers[PUBLIC_NAME] = [PUBLIC_ADDRESS, PRIVATE_ADDRESS]

    result = _channel().send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert _FakeSMTP.instances == []


def test_unresolvable_name_is_refused_without_connecting(resolver):
    result = _channel("smtp.nowhere.example").send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert result.reason == REFUSED_TOKEN
    assert _FakeSMTP.instances == []


@pytest.mark.parametrize(
    "literal", ["127.0.0.1", "169.254.169.254", "::ffff:127.0.0.1"]
)
def test_literal_internal_address_is_refused_without_dns(resolver, literal):
    result = _channel(literal).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert resolver.lookups == []
    assert _FakeSMTP.instances == []


@pytest.mark.parametrize("name", ["localhost", "LOCALHOST.", "api.localhost"])
def test_localhost_names_are_refused_without_dns(resolver, name):
    result = _channel(name).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert resolver.lookups == []
    assert _FakeSMTP.instances == []


@pytest.mark.parametrize(
    "host", ["10.0.0.5@smtp.public.example", "mail/relay", "a b.example"]
)
def test_malformed_host_is_refused_before_any_lookup(resolver, host):
    result = _channel(host).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert resolver.lookups == []
    assert _FakeSMTP.instances == []


def test_unlisted_host_with_same_internal_address_is_refused(resolver, monkeypatch):
    monkeypatch.setenv(ALLOW, "relay.other.example")
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    result = _channel(PRIVATE_NAME).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert _FakeSMTP.instances == []


def test_unset_allow_list_refuses_internal_host(resolver):
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    result = _channel(PRIVATE_NAME).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert _FakeSMTP.instances == []


def test_listed_host_that_does_not_resolve_is_refused(resolver, monkeypatch):
    # Opting a host in lifts the internal-address rule only; a lookup with no
    # answers still refuses.
    monkeypatch.setenv(ALLOW, PRIVATE_NAME)
    resolver.answers[PRIVATE_NAME] = []

    result = _channel(PRIVATE_NAME).send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert _FakeSMTP.instances == []


def test_legacy_sender_refuses_internal_host_without_connecting(resolver, monkeypatch):
    monkeypatch.setenv(HOST, PRIVATE_NAME)
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    assert ne.send_notification_email(RECORD) is False
    assert _FakeSMTP.instances == []


def test_legacy_sender_refuses_unresolvable_host_without_connecting(
    resolver, monkeypatch
):
    monkeypatch.setenv(HOST, "smtp.nowhere.example")

    assert ne.send_notification_email(RECORD) is False
    assert _FakeSMTP.instances == []


# ---------------------------------------------------------------------------
# Operator opt-in: validation of the allow list
# ---------------------------------------------------------------------------


def test_empty_allow_list_logs_no_allow_list_warning(resolver, monkeypatch, caplog):
    monkeypatch.setenv(ALLOW, "")
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    with caplog.at_level(logging.WARNING, logger=ne.__name__):
        _channel(PRIVATE_NAME).send(RECORD)

    assert not [r for r in caplog.records if ALLOW in r.getMessage()]


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        # A blank entry between two good ones: the good ones still count.
        (f"{PRIVATE_NAME},,relay.other.example", DeliveryStatus.DELIVERED),
        # A trailing comma leaves a blank entry; the malformed one is dropped.
        (f"{PRIVATE_NAME},bad host/x,", DeliveryStatus.DELIVERED),
    ],
)
def test_blank_or_malformed_entries_warn_once_naming_variable_not_value(
    resolver, monkeypatch, caplog, value, expected_status
):
    monkeypatch.setenv(ALLOW, value)
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    with caplog.at_level(logging.WARNING, logger=ne.__name__):
        result = _channel(PRIVATE_NAME).send(RECORD)

    assert result.status is expected_status
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and ALLOW in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "bad host" not in _all_log_text(caplog)


def test_malformed_entry_does_not_allow_its_host(resolver, monkeypatch):
    monkeypatch.setenv(ALLOW, "bad host/x")
    resolver.answers["bad host/x"] = [PRIVATE_ADDRESS]

    result = _channel("bad host/x").send(RECORD)

    assert result.status is DeliveryStatus.REJECTED
    assert _FakeSMTP.instances == []


# ---------------------------------------------------------------------------
# Logs never carry the host or the address
# ---------------------------------------------------------------------------


def test_refusal_logs_channel_name_and_constant_reason_only(resolver, caplog):
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    with caplog.at_level(logging.DEBUG):
        _channel(PRIVATE_NAME).send(RECORD)

    text = _all_log_text(caplog)
    assert "notify" in text
    assert REFUSED_TOKEN in text
    assert PRIVATE_NAME not in text
    assert PRIVATE_ADDRESS not in text


def test_legacy_refusal_logs_no_host_or_address(resolver, monkeypatch, caplog):
    monkeypatch.setenv(HOST, PRIVATE_NAME)
    resolver.answers[PRIVATE_NAME] = [PRIVATE_ADDRESS]

    with caplog.at_level(logging.DEBUG):
        ne.send_notification_email(RECORD)

    text = _all_log_text(caplog)
    assert PRIVATE_NAME not in text
    assert PRIVATE_ADDRESS not in text
