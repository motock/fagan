"""Tests for ``pipeline.notification_email.send_notification_email``.

Contract under test (PLANNOTIFY-05):

* ``send_notification_email(record: dict) -> bool`` sends ONE spooled outbox
  record as an email over SMTP and NEVER raises (True on success, False on
  any failure).
* All configuration comes from ``os.environ`` and is read at CALL time (so
  tests may ``monkeypatch.setenv`` after import).
* The feature is disabled by default and fails closed on partial config.
* The configured password must never reach a log record, an exception that
  escapes, or a return value.

Every test mocks at the external boundary ONLY: ``smtplib.SMTP`` is replaced
by a recording fake.  No test in this file opens a real socket.

Contract decisions made explicit for the implementer:

* A non-numeric ``PIPELINE_NOTIFY_EMAIL_TIMEOUT`` fails CLOSED (returns
  False), mirroring the non-numeric ``PIPELINE_NOTIFY_EMAIL_PORT`` rule,
  rather than silently falling back to the default.
* On success at least one log record mentions the outcome ("sent"); on the
  fail-closed path an ERROR record is emitted.  Payloads (body, recipient
  list) never appear at INFO or below.
"""

from __future__ import annotations

import email.message
import inspect
import logging
import smtplib
import ssl
from typing import ClassVar

import pytest

from pipeline import config_provenance, notification_email
from pipeline.notification_email import send_notification_email

ENABLED = "PIPELINE_NOTIFY_EMAIL_ENABLED"
HOST = "PIPELINE_NOTIFY_EMAIL_HOST"
PORT = "PIPELINE_NOTIFY_EMAIL_PORT"
USER = "PIPELINE_NOTIFY_EMAIL_USER"
PASSWORD = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
FROM = "PIPELINE_NOTIFY_EMAIL_FROM"
TO = "PIPELINE_NOTIFY_EMAIL_TO"
TIMEOUT = "PIPELINE_NOTIFY_EMAIL_TIMEOUT"

ALL_VARS = (ENABLED, HOST, PORT, USER, PASSWORD, FROM, TO, TIMEOUT)

# The eight names this story must register in the provenance catalog.
CATALOG_NAMES = ALL_VARS

# A pre-existing catalog entry that the notify group must be appended AFTER
# (the brief says the new commented group goes at the END of the list).
CATALOG_TAIL_ANCHOR = "PIPELINE_CLOUD_NUM_CTX"

BASE_ENV = {
    ENABLED: "1",
    HOST: "smtp.example.test",
    PORT: "2525",
    USER: "mailer@example.test",
    PASSWORD: "pw-sentinel-value",
    FROM: "pipeline@example.test",
    TO: "sink-recipient@example.test",
    TIMEOUT: "7",
}


def _record(**overrides):
    record = {"plan": "PLAN-42", "message": "Plan finished; 12 stories merged."}
    record.update(overrides)
    return record


def _set_env(monkeypatch, **overrides):
    """Start from a clean slate for the eight vars, then apply BASE_ENV.

    An override whose value is None leaves that variable unset, which is how
    tests express "this knob is missing from the environment".
    """
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    values = dict(BASE_ENV)
    values.update(overrides)
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def _install_fake(monkeypatch):
    """Point ``smtplib.SMTP`` (the external boundary) at the recording fake."""
    # Per-test isolation: drop recordings left by a previous test in this
    # worker (the class-level list would otherwise leak across tests).
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    # Tolerate an implementation that does ``from smtplib import SMTP``.
    monkeypatch.setattr(notification_email, "SMTP", FakeSMTP, raising=False)
    return FakeSMTP


def _require_getaddrinfo_port(port):
    """Mirror the real boundary: socket.getaddrinfo rejects a bad port."""
    try:
        int(port)
    except (TypeError, ValueError):
        raise OSError(f"getaddrinfo failed for non-numeric port {port!r}") from None


def _require_numeric_timeout(timeout):
    """Mirror the real boundary: sock.settimeout rejects a non-number."""
    if timeout is None:
        return
    if not isinstance(timeout, (int, float)):
        raise TypeError(f"socket timeout must be numeric, got {timeout!r}")


class FakeSMTP:
    """Recording stand-in for ``smtplib.SMTP``; never touches a socket.

    The ``*_error`` class attributes let a test make a chosen step fail the
    way the real boundary would (an instance created inside the code under
    test picks them up via class lookup).
    """

    instances: ClassVar[list] = []
    ctor_error: ClassVar[BaseException | None] = None
    starttls_error: ClassVar[BaseException | None] = None
    login_error: ClassVar[BaseException | None] = None
    send_message_error: ClassVar[BaseException | None] = None

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        self.events = ["ctor"]
        self.starttls_calls = 0
        self.login_calls = []
        self.send_message_calls = []
        self.quit_called = False
        self.exit_called = False
        FakeSMTP.instances.append(self)
        _require_getaddrinfo_port(self.ctor_port())
        _require_numeric_timeout(self.ctor_timeout())
        if self.ctor_error is not None:
            raise self.ctor_error

    # -- constructor argument access, tolerant of positional vs kwarg -----
    def ctor_values(self):
        args, kwargs = self.init_args, self.init_kwargs
        host = args[0] if len(args) > 0 else kwargs.get("host", "")
        port = args[1] if len(args) > 1 else kwargs.get("port", 0)
        if "timeout" in kwargs:
            timeout = kwargs["timeout"]
        elif len(args) > 2:
            timeout = args[2]
        else:
            timeout = None
        return host, port, timeout

    def ctor_port(self):
        """The port exactly as the constructor received it."""
        return self.ctor_values()[1]

    def ctor_timeout(self):
        """The timeout exactly as the constructor received it."""
        return self.ctor_values()[2]

    @property
    def closed(self):
        """True once the connection was closed by either supported style."""
        return self.quit_called or self.exit_called

    # -- smtplib.SMTP surface ---------------------------------------------
    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exit_called = True
        return False

    def starttls(self, *args, **kwargs):
        self.events.append("starttls")
        self.starttls_calls += 1
        if self.starttls_error is not None:
            raise self.starttls_error

    def login(self, *args, **kwargs):
        self.events.append("login")
        self.login_calls.append((args, kwargs))
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, *args, **kwargs):
        self.events.append("send_message")
        self.send_message_calls.append((args, kwargs))
        if self.send_message_error is not None:
            raise self.send_message_error

    def quit(self):
        self.events.append("quit")
        self.quit_called = True
        return (221, b"bye")


def _sent_message(fake):
    """Return the EmailMessage the fake saw in ``send_message``."""
    assert fake.send_message_calls, "send_message was never called"
    args, kwargs = fake.send_message_calls[0]
    for obj in list(args) + list(kwargs.values()):
        if isinstance(obj, email.message.EmailMessage):
            return obj
    pytest.fail("send_message was not called with an email.message.EmailMessage")


def _body_of(message):
    """Extract the body text whether set via set_content or set_payload."""
    try:
        return message.get_content()
    except Exception:  # noqa: BLE001 - probe of unknown payload style
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode()
        return payload or ""


# ---------------------------------------------------------------------------
# Positive paths
# ---------------------------------------------------------------------------


def test_success_returns_true_and_sends_expected_email(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is True
    assert len(fake_cls.instances) == 1
    fake = fake_cls.instances[0]
    host, port, timeout = fake.ctor_values()
    assert host == "smtp.example.test"
    assert int(port) == 2525
    assert timeout == 7
    assert fake.starttls_calls == 1
    assert len(fake.login_calls) == 1
    login_args, login_kwargs = fake.login_calls[0]
    flat = list(login_args) + list(login_kwargs.values())
    assert BASE_ENV[USER] in flat
    assert BASE_ENV[PASSWORD] in flat
    assert len(fake.send_message_calls) == 1
    message = _sent_message(fake)
    assert isinstance(message, email.message.EmailMessage)
    assert message["From"] == "pipeline@example.test"
    assert message["To"] == "sink-recipient@example.test"
    assert message["Subject"] == "[pipeline] plan complete: PLAN-42"
    # Order mandated by the brief: connect -> starttls -> login -> send.
    assert (
        fake.events.index("starttls")
        < fake.events.index("login")
        < fake.events.index("send_message")
    )
    assert fake.closed


def test_timeout_value_is_passed_to_smtp_constructor(monkeypatch):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_TIMEOUT="11")
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is True
    _, _, timeout = fake_cls.instances[0].ctor_values()
    assert timeout == 11


def test_subject_contains_plan_and_body_contains_record_message(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(
        {"plan": "PLAN-99", "message": "Story 12 merged; oracle clean."}
    )

    assert result is True
    message = _sent_message(fake_cls.instances[0])
    assert message["Subject"] == "[pipeline] plan complete: PLAN-99"
    assert "Story 12 merged; oracle clean." in _body_of(message)


def test_from_falls_back_to_user_when_from_empty(monkeypatch):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_FROM=None)
    fake_cls = _install_fake(monkeypatch)

    assert send_notification_email(_record()) is True
    assert _sent_message(fake_cls.instances[-1])["From"] == "mailer@example.test"

    # The reverse priority: an explicit FROM wins over USER.
    fake_cls.instances.clear()
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_FROM="explicit@example.test")
    assert send_notification_email(_record()) is True
    assert _sent_message(fake_cls.instances[-1])["From"] == "explicit@example.test"


def test_port_and_timeout_defaults_applied_when_unset(monkeypatch):
    _set_env(
        monkeypatch,
        PIPELINE_NOTIFY_EMAIL_PORT=None,
        PIPELINE_NOTIFY_EMAIL_TIMEOUT=None,
    )
    fake_cls = _install_fake(monkeypatch)

    assert send_notification_email(_record()) is True
    _, port, timeout = fake_cls.instances[0].ctor_values()
    assert int(port) == 587
    assert timeout == 20


def test_empty_password_is_not_required_for_send(monkeypatch):
    # The fail-closed rule names HOST, TO and FROM/USER only; an empty
    # PASSWORD must not block the send.
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_PASSWORD="")
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is True
    assert len(fake_cls.instances[0].login_calls) == 1


# ---------------------------------------------------------------------------
# Disabled / fail-closed paths
# ---------------------------------------------------------------------------


def test_disabled_by_default_returns_false_without_constructing_smtp(monkeypatch):
    # Every other knob is configured; only the ENABLED gate is missing.
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_ENABLED=None)
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []


@pytest.mark.parametrize("value", ["0", "", "false", "true", "yes", "2"])
def test_enabled_values_other_than_one_never_send(monkeypatch, value):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_ENABLED=value)
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []


def test_fail_closed_when_host_missing(monkeypatch, caplog):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_HOST=None)
    fake_cls = _install_fake(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_fail_closed_when_host_empty_string(monkeypatch, caplog):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_HOST="")
    fake_cls = _install_fake(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("to_value", [None, ""])
def test_fail_closed_when_to_missing(monkeypatch, caplog, to_value):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_TO=to_value)
    fake_cls = _install_fake(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize(
    "overrides",
    [
        {FROM: None, USER: None},
        {FROM: "", USER: ""},
    ],
)
def test_fail_closed_when_from_and_user_both_empty(monkeypatch, caplog, overrides):
    _set_env(monkeypatch, **overrides)
    fake_cls = _install_fake(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Failure containment: never raises, always closes
# ---------------------------------------------------------------------------


def test_smtp_constructor_oserror_returns_false_without_raising(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        fake_cls, "ctor_error", OSError("connection refused"), raising=False
    )

    result = send_notification_email(_record())

    assert result is False


def test_login_authentication_error_returns_false_and_closes(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        fake_cls,
        "login_error",
        smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed"),
        raising=False,
    )

    result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances, "SMTP was never constructed"
    assert fake_cls.instances[0].closed


def test_send_message_failure_returns_false_and_still_closes(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        fake_cls,
        "send_message_error",
        RuntimeError("smtp connection dropped mid-send"),
        raising=False,
    )

    result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances, "SMTP was never constructed"
    assert fake_cls.instances[0].closed


def test_non_numeric_port_returns_false_without_raising(monkeypatch):
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_PORT="abc")
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is False
    assert not any(f.send_message_calls for f in fake_cls.instances)


def test_non_numeric_timeout_fails_closed_without_raising(monkeypatch):
    # Contract decision: a non-numeric TIMEOUT fails closed (returns False),
    # mirroring the non-numeric PORT rule, instead of falling back to 20.
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_TIMEOUT="soon")
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email(_record())

    assert result is False
    assert not any(f.send_message_calls for f in fake_cls.instances)


def test_record_missing_message_sends_empty_body(monkeypatch):
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)

    result = send_notification_email({"plan": "PLAN-7"})

    assert result is True
    assert _body_of(_sent_message(fake_cls.instances[0])).strip() == ""


# ---------------------------------------------------------------------------
# Security: the credential never leaks
# ---------------------------------------------------------------------------


def test_password_never_appears_in_any_log_record_on_login_failure(
    monkeypatch, caplog
):
    sentinel = "sup3r-secret-password-value"
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_PASSWORD=sentinel)
    fake_cls = _install_fake(monkeypatch)
    monkeypatch.setattr(
        fake_cls,
        "login_error",
        smtplib.SMTPAuthenticationError(
            535,
            f"5.7.8 Error: authentication failed, password {sentinel} rejected",
        ),
        raising=False,
    )

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email(_record())

    assert result is False
    assert fake_cls.instances[0].closed
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in (record.exc_text or "")
        if record.exc_info is not None:
            assert sentinel not in str(record.exc_info[1])
    # The failure is still diagnosable: the exception TYPE is logged.
    assert any("SMTPAuthenticationError" in r.getMessage() for r in caplog.records)


def test_success_logs_outcome_not_payloads(monkeypatch, caplog):
    body_marker = "BODY-MARKER-do-not-log-0xdeadbeef"
    recipient = "quiet-recipient@example.test"
    _set_env(monkeypatch, PIPELINE_NOTIFY_EMAIL_TO=recipient)
    _install_fake(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = send_notification_email({"plan": "PLAN-5", "message": body_marker})

    assert result is True
    for record in caplog.records:
        if record.levelno <= logging.INFO:
            assert body_marker not in record.getMessage()
            assert recipient not in record.getMessage()
    # Outcomes are logged ("sent"), not payloads.
    assert any(
        "sent" in r.getMessage().lower() or "send" in r.getMessage().lower()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Signature and config_provenance catalog registration
# ---------------------------------------------------------------------------


def test_function_takes_a_single_record_parameter():
    params = list(inspect.signature(send_notification_email).parameters)
    assert params == ["record"]


def test_catalog_registers_all_eight_notify_vars():
    specs = {spec.name: spec for spec in config_provenance.ENV_VAR_CATALOG}
    for name in CATALOG_NAMES:
        assert name in specs, f"{name} missing from ENV_VAR_CATALOG"
    assert specs[ENABLED].default == "0"
    assert specs[PORT].default == "587"
    assert specs[TIMEOUT].default == "20"
    for name in (HOST, USER, PASSWORD, FROM, TO):
        assert specs[name].default in ("", None)


def test_catalog_notify_group_appended_after_existing_tail():
    order = [spec.name for spec in config_provenance.ENV_VAR_CATALOG]
    anchor_index = order.index(CATALOG_TAIL_ANCHOR)
    for name in CATALOG_NAMES:
        assert order.index(name) > anchor_index


def test_password_var_is_masked_as_secret_by_provenance():
    assert config_provenance._is_secret(PASSWORD) is True
    assert config_provenance._is_secret(HOST) is False


# ---------------------------------------------------------------------------
# Regression tests added from code review (REQUEST_CHANGES)
#
# Both tests build the record the way the outbox spool ACTUALLY persists it:
# pipeline/notification_outbox.py writes ``record = dict(event)`` where
# ``event`` is pipeline.events.make_event's bus-level shape
# ``{"type", "plan", "story_key", "payload": {...}, "ts"}``.  The
# pre-existing tests above only exercise legacy flat ``{"plan", "message"}``
# records, which is why neither blocking finding was caught in CI.
# No existing test was modified or removed to add these.
# ---------------------------------------------------------------------------


def test_sender_reads_message_from_spooled_bus_event_payload(monkeypatch):
    """Blocking 1: the body must come from ``record["payload"]["message"]``.

    The spooled outbox record is the bus-level event (``dict(event)`` of
    make_event's shape), so the message a real send must carry lives at
    ``record["payload"]["message"]`` -- NOT at ``record["message"]``.  A
    sender that reads the top-level key sends every real spooled record with
    an EMPTY body while the subject (which reads the top-level ``plan``)
    still renders correctly, which is exactly why this survived eyeball
    review of the flat-record tests.
    """
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)

    # Exactly the shape notification_outbox.py spools (make_event via
    # ``record = dict(event)``); the payload fields must round-trip because
    # "the later out-of-band send needs them".
    spooled_record = {
        "type": "notification.email",
        "plan": "pro",
        "story_key": "STORY-7",
        "payload": {
            "to": "ops@example.com",
            "message": "Plan pro: 12/15 stories complete",
        },
        "ts": "2025-06-01T12:00:00Z",
    }

    result = send_notification_email(spooled_record)

    assert result is True
    message = _sent_message(fake_cls.instances[-1])
    # The subject reads the bus-event's top-level "plan" and already worked;
    # the BODY is the regression under test.
    assert message["Subject"] == "[pipeline] plan complete: pro"
    assert (
        _body_of(message).strip() == "Plan pro: 12/15 stories complete"
    ), "body must be the spooled record's payload message, not the empty top-level fallback of a bus-event record"


class _StarttlsRecordingSMTP(FakeSMTP):
    """``FakeSMTP`` plus recording of ``starttls`` arguments.

    The shared ``FakeSMTP`` above is frozen (every pre-existing test depends
    on it verbatim) and discards ``starttls`` arguments, so this LOCAL
    subclass records them under a NEW attribute rather than editing the
    shared fake in any way.
    """

    def starttls(self, *args, **kwargs):
        if not hasattr(self, "starttls_args_record"):
            self.starttls_args_record = []
        self.starttls_args_record.append((args, kwargs))
        super().starttls(*args, **kwargs)


def test_starttls_is_called_with_a_verifying_ssl_context(monkeypatch):
    """Blocking 2: ``starttls()`` must pass a certificate-verifying context.

    A bare ``smtp.starttls()`` makes smtplib supply
    ``ssl._create_stdlib_context()`` (CERT_NONE, check_hostname=False), so
    the credential handed to the very next ``login()`` crosses an
    unauthenticated TLS channel.  The sender must pass
    ``context=ssl.create_default_context()`` (CERT_REQUIRED + hostname
    check) so an active MITM's certificate fails verification and the send
    fails closed BEFORE the password is transmitted.
    """
    _set_env(monkeypatch)
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _StarttlsRecordingSMTP)
    # Tolerate an implementation that does ``from smtplib import SMTP``.
    monkeypatch.setattr(
        notification_email, "SMTP", _StarttlsRecordingSMTP, raising=False
    )

    assert send_notification_email(_record()) is True

    fake = FakeSMTP.instances[-1]
    assert fake.starttls_calls == 1
    recorded = getattr(fake, "starttls_args_record", [])
    assert recorded, "starttls was never called"
    args, kwargs = recorded[0]
    # Accept the context positionally or by keyword; only its VERIFICATION
    # properties are the contract, not the call syntax.
    context = kwargs.get("context") if kwargs else None
    if context is None and args:
        context = args[0]
    assert isinstance(context, ssl.SSLContext), (
        "smtp.starttls() must be called with an ssl.SSLContext (e.g. "
        "context=ssl.create_default_context()); a bare starttls() lets "
        "smtplib use the unverified stdlib context (CERT_NONE, no hostname "
        "check)"
    )
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True