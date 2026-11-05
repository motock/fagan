"""Notification channels take secrets from the credential seam (refs).

A channel instance may name a credential reference (``password_ref`` /
``webhook_url_ref``) instead of an environment variable
(``password_env`` / ``webhook_url_env``).  The reference is resolved through
``app.credentials.resolve`` at send time, so an operator-supplied provider can
supply the value without a core change per backend.

Only the HTTP opener boundary (``_build_opener``) and ``smtplib.SMTP`` are
faked, reusing the fakes from the sibling channel tests.
"""

from __future__ import annotations

import json
import logging
import smtplib
import textwrap
from typing import ClassVar

import pytest

import app.credentials as creds
from pipeline import notification_channels as nc
from pipeline import notification_email as ne
from pipeline import notification_slack as ns

# Made-up credential values.  None of them may reach a log record, an
# exception message, a repr or an assertion message.
SMTP_SECRET = "made-up-smtp-secret-9f3a"
SLACK_PLUGIN_URL = "https://hooks.slack.test/services/T000/B000/PLUGINTOKEN"
SLACK_ENV_URL = "https://hooks.slack.test/services/T000/B000/ENVTOKEN"

SMTP_PASSWORD_ENV = "REF_TEST_SMTP_PW"
SLACK_HOOK_ENV = "REF_TEST_SLACK_HOOK"

RECORD = {
    "plan": "PLAN-1",
    "payload": {"message": "hello world", "event": "plan_completed"},
}


# ---- fakes (mirroring the sibling channel tests) ------------------------------


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, outcome=200):
        self.outcome = outcome
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return FakeResponse(self.outcome)


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host, port, timeout=None):
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
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _clean_seam(monkeypatch):
    """No provider is configured unless a test configures one."""
    creds._PLUGIN_PROVIDERS.clear()
    monkeypatch.delenv("PIPELINE_CREDENTIALS_PLUGIN", raising=False)
    yield
    creds._PLUGIN_PROVIDERS.clear()


@pytest.fixture
def opener(monkeypatch):
    holder = {"opener": FakeOpener()}
    monkeypatch.setattr(ns, "_build_opener", lambda: holder["opener"])
    monkeypatch.setenv(SLACK_HOOK_ENV, SLACK_ENV_URL)
    return holder


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv(SMTP_PASSWORD_ENV, SMTP_SECRET)
    return FakeSMTP


# ---- operator-supplied providers ---------------------------------------------

_VALUE_PLUGIN = """
import app.credentials as creds


class Provider:
    def get(self, ref):
        return creds.Credential(creds.SecretValue({value!r}))


def factory():
    return Provider()
"""

_NONE_PLUGIN = """
class Provider:
    def get(self, ref):
        return None


def factory():
    return Provider()
"""

_RAISING_PLUGIN = """
import app.credentials as creds


class Provider:
    def get(self, ref):
        raise creds.CredentialError("provider unavailable")


def factory():
    return Provider()
"""

_SEQUENCE_PLUGIN = """
import app.credentials as creds

_values = [{first!r}, {second!r}]
_calls = []


class Provider:
    def get(self, ref):
        index = len(_calls)
        _calls.append(ref.name)
        if index >= len(_values) or _values[index] is None:
            return None
        return creds.Credential(creds.SecretValue(_values[index]))


def factory():
    return Provider()
"""


def _install_plugin(tmp_path, monkeypatch, filename, source):
    """Write a provider plugin into ``tmp_path`` and select it by env var."""
    (tmp_path / f"{filename}.py").write_text(
        textwrap.dedent(source).lstrip(), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PIPELINE_CREDENTIALS_PLUGIN", f"{filename}:factory")


def _plugin_returning(tmp_path, monkeypatch, filename, value):
    _install_plugin(tmp_path, monkeypatch, filename, _VALUE_PLUGIN.format(value=value))


# ---- settings builders --------------------------------------------------------


def _slack(**overrides):
    settings = {"type": "slack", "timeout": 20, **overrides}
    return ns.SlackChannelType.from_settings("ref-slack", settings, {})


def _email(**overrides):
    settings = {
        "type": "email",
        "host": "smtp.example.test",
        "port": 587,
        "user": "notifier",
        "from": "pipeline@example.test",
        "to": ["team@example.test"],
        **overrides,
    }
    return ne.EmailChannelType.from_settings("ref-mail", settings, {})


# ---- Slack: webhook_url_ref ---------------------------------------------------


def test_slack_posts_to_the_url_named_by_a_ref(tmp_path, monkeypatch, opener, caplog):
    caplog.set_level(logging.DEBUG)
    _plugin_returning(tmp_path, monkeypatch, "creds_ref_slack_ok", SLACK_PLUGIN_URL)

    result = _slack(webhook_url_ref="tenant.slack").send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert [r.full_url for r in opener["opener"].requests] == [SLACK_PLUGIN_URL]
    assert "PLUGINTOKEN" not in caplog.text
    assert "ENVTOKEN" not in caplog.text


def test_slack_still_posts_to_the_environment_url_without_a_ref(opener):
    result = _slack(webhook_url_env=SLACK_HOOK_ENV).send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert [r.full_url for r in opener["opener"].requests] == [SLACK_ENV_URL]


def test_slack_rejects_both_a_ref_and_an_env_name():
    with pytest.raises(nc.ChannelConfigError, match="mutually exclusive"):
        _slack(webhook_url_env=SLACK_HOOK_ENV, webhook_url_ref="tenant.slack")


def test_slack_requires_one_of_ref_or_env_with_the_existing_message():
    with pytest.raises(nc.ChannelConfigError) as err:
        _slack()

    assert "setting 'webhook_url_env' must name an env var" in str(err.value)


@pytest.mark.parametrize("bad", ["Bad Ref", "", 42])
def test_slack_rejects_an_invalid_ref(bad):
    with pytest.raises(nc.ChannelConfigError):
        _slack(webhook_url_ref=bad)


def test_slack_ref_error_does_not_echo_the_ref():
    with pytest.raises(nc.ChannelConfigError) as err:
        _slack(webhook_url_ref="tenant slack")

    assert "tenant slack" not in str(err.value)


def test_slack_ref_resolving_to_none_behaves_like_an_unset_env_var(
    tmp_path, monkeypatch, opener, caplog
):
    caplog.set_level(logging.DEBUG)

    # Baseline: today's unset env var.
    monkeypatch.setenv(SLACK_HOOK_ENV, "")
    baseline = _slack(webhook_url_env=SLACK_HOOK_ENV).send(RECORD)
    baseline_text = caplog.text
    caplog.clear()

    _install_plugin(tmp_path, monkeypatch, "creds_ref_slack_none", _NONE_PLUGIN)
    resolved = _slack(webhook_url_ref="tenant.slack").send(RECORD)

    assert resolved.status is baseline.status is nc.DeliveryStatus.RETRY
    assert resolved.reason == baseline.reason
    assert opener["opener"].requests == []
    assert "tenant.slack" in caplog.text
    assert "misconfigured" in caplog.text
    assert baseline_text != ""


def test_slack_second_send_uses_a_value_the_first_send_did_not_have(
    tmp_path, monkeypatch, opener
):
    _install_plugin(
        tmp_path,
        monkeypatch,
        "creds_ref_slack_sequence",
        _SEQUENCE_PLUGIN.format(first=None, second=SLACK_PLUGIN_URL),
    )
    channel = _slack(webhook_url_ref="tenant.slack")

    assert channel.send(RECORD).status is nc.DeliveryStatus.RETRY
    assert channel.send(RECORD).status is nc.DeliveryStatus.DELIVERED
    assert [r.full_url for r in opener["opener"].requests] == [SLACK_PLUGIN_URL]


def test_slack_retries_without_posting_when_the_provider_fails(
    tmp_path, monkeypatch, opener, caplog
):
    caplog.set_level(logging.DEBUG)
    _install_plugin(tmp_path, monkeypatch, "creds_ref_slack_raise", _RAISING_PLUGIN)

    result = _slack(webhook_url_ref="tenant.slack").send(RECORD)

    assert result.status is nc.DeliveryStatus.RETRY
    assert opener["opener"].requests == []
    # The failure is logged as the exception TYPE only; the ambient
    # environment URL is never posted as a fallback.
    assert "CredentialError" in caplog.text
    assert "ENVTOKEN" not in caplog.text


# ---- email: password_ref ------------------------------------------------------


def test_email_logs_in_with_the_password_named_by_a_ref(
    tmp_path, monkeypatch, fake_smtp, caplog
):
    caplog.set_level(logging.DEBUG)
    _plugin_returning(tmp_path, monkeypatch, "creds_ref_email_ok", SMTP_SECRET)

    result = _email(password_ref="tenant.smtp").send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert fake_smtp.instances[0].login_args == ("notifier", SMTP_SECRET)
    assert SMTP_SECRET not in caplog.text


def test_email_still_reads_the_password_from_the_named_env_var(fake_smtp):
    result = _email(password_env=SMTP_PASSWORD_ENV).send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert fake_smtp.instances[0].login_args == ("notifier", SMTP_SECRET)


def test_email_rejects_both_a_ref_and_an_env_name():
    with pytest.raises(nc.ChannelConfigError) as err:
        _email(password_env=SMTP_PASSWORD_ENV, password_ref="tenant.smtp")

    assert "settings password_env and password_ref are mutually exclusive" in str(
        err.value
    )


@pytest.mark.parametrize("bad", ["Bad Ref", "", 42, "-leading", "tenant\u00e9"])
def test_email_rejects_an_invalid_ref(bad):
    with pytest.raises(nc.ChannelConfigError):
        _email(password_ref=bad)


def test_email_ref_resolving_to_none_behaves_like_an_unset_env_var(
    tmp_path, monkeypatch, fake_smtp
):
    monkeypatch.delenv(SMTP_PASSWORD_ENV, raising=False)
    unset_env = _email(password_env=SMTP_PASSWORD_ENV).send(RECORD)
    unset_login = fake_smtp.instances[0].login_args

    _install_plugin(tmp_path, monkeypatch, "creds_ref_email_none", _NONE_PLUGIN)
    resolved = _email(password_ref="tenant.smtp").send(RECORD)

    assert resolved.status is unset_env.status is nc.DeliveryStatus.DELIVERED
    assert unset_login == ("notifier", "")
    assert fake_smtp.instances[1].login_args == ("notifier", "")


def test_email_second_send_uses_a_password_the_first_send_did_not_have(
    tmp_path, monkeypatch, fake_smtp
):
    _install_plugin(
        tmp_path,
        monkeypatch,
        "creds_ref_email_sequence",
        _SEQUENCE_PLUGIN.format(first=None, second=SMTP_SECRET),
    )
    channel = _email(password_ref="tenant.smtp")

    channel.send(RECORD)
    channel.send(RECORD)

    assert fake_smtp.instances[0].login_args == ("notifier", "")
    assert fake_smtp.instances[1].login_args == ("notifier", SMTP_SECRET)


def test_email_retries_without_connecting_when_the_provider_fails(
    tmp_path, monkeypatch, fake_smtp, caplog
):
    caplog.set_level(logging.DEBUG)
    _install_plugin(tmp_path, monkeypatch, "creds_ref_email_raise", _RAISING_PLUGIN)

    result = _email(password_ref="tenant.smtp").send(RECORD)

    assert result.status is nc.DeliveryStatus.RETRY
    assert fake_smtp.instances == []
    assert SMTP_SECRET not in caplog.text


def test_email_without_any_password_setting_sends_with_an_empty_password(fake_smtp):
    result = _email().send(RECORD)

    assert result.status is nc.DeliveryStatus.DELIVERED
    assert fake_smtp.instances[0].login_args == ("notifier", "")


# ---- reject_inline_secrets ----------------------------------------------------


def test_inline_secret_is_still_rejected():
    with pytest.raises(nc.ChannelConfigError) as err:
        nc.reject_inline_secrets("s", {"password": "hunter2"})

    assert "hunter2" not in str(err.value)
    assert "must not hold a secret inline" in str(err.value)


@pytest.mark.parametrize("key", ["password_ref", "webhook_url_ref", "token_ref"])
def test_reference_keys_are_allowed(key):
    nc.reject_inline_secrets("s", {key: "tenant.smtp"})


def test_a_key_merely_containing_ref_is_still_rejected():
    with pytest.raises(nc.ChannelConfigError):
        nc.reject_inline_secrets("s", {"password_ref_extra": "x"})


def test_slack_ref_payload_is_still_the_rendered_text(tmp_path, monkeypatch, opener):
    _plugin_returning(tmp_path, monkeypatch, "creds_ref_slack_payload", SLACK_PLUGIN_URL)

    _slack(webhook_url_ref="tenant.slack").send(RECORD)

    body = json.loads(opener["opener"].requests[0].data)
    assert "hello world" in body["text"]
