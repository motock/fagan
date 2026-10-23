"""Tests for the notification channel type contract and registry."""

import logging
from pathlib import Path

import pytest

from pipeline import notification_channels as nc
from pipeline import paths


class FakeChannel:
    type_name = "fake_nc02"

    def __init__(self, name, result=None, error=None):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self._result = result
        self._error = error

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        nc.reject_inline_secrets(instance_name, settings)
        return cls(instance_name)

    def send(self, record):
        if self._error:
            raise self._error
        return self._result


class EnvChannel(FakeChannel):
    type_name = "fake_env_nc02"

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        nc.reject_inline_secrets(instance_name, settings)
        return cls(instance_name)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(nc, "_REGISTRY", {})
    return nc


def test_should_return_true_for_delivered(registry):
    ch = FakeChannel("a", nc.DeliveryResult(nc.DeliveryStatus.DELIVERED))
    assert nc.as_sender(ch)({}) is True


def test_should_return_false_for_retry(registry):
    ch = FakeChannel("a", nc.DeliveryResult(nc.DeliveryStatus.RETRY, 5.0))
    assert nc.as_sender(ch)({}) is False


def test_should_drop_rejected_and_log_error_once(registry, caplog):
    ch = FakeChannel(
        "inst", nc.DeliveryResult(nc.DeliveryStatus.REJECTED, reason="bad_auth")
    )
    with caplog.at_level(logging.DEBUG):
        assert nc.as_sender(ch)({"body": "TOP-SECRET-CONTENT"}) is True
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    text = errors[0].getMessage()
    assert "inst" in text and "bad_auth" in text
    assert "TOP-SECRET-CONTENT" not in caplog.text


def test_should_return_false_and_log_only_type_when_send_raises(registry, caplog):
    ch = FakeChannel("a", error=RuntimeError("https://hook.example/xyz"))
    with caplog.at_level(logging.DEBUG):
        assert nc.as_sender(ch)({}) is False
    assert "RuntimeError" in caplog.text
    assert "hook.example" not in caplog.text


def test_should_register_and_look_up_type(registry):
    nc.register_channel_type(FakeChannel)
    assert nc.channel_type("fake_nc02") is FakeChannel
    assert "fake_nc02" in nc.registered_types()


def test_should_reject_duplicate_type(registry):
    nc.register_channel_type(FakeChannel)
    with pytest.raises(ValueError):
        nc.register_channel_type(FakeChannel)


@pytest.mark.parametrize("bad", ["", "Upper", "1abc", "has-dash", "a" * 33])
def test_should_reject_invalid_type_name(registry, bad):
    cls = type("Bad", (FakeChannel,), {"type_name": bad})
    with pytest.raises(ValueError):
        nc.register_channel_type(cls)


def test_should_raise_keyerror_naming_unknown_type(registry):
    with pytest.raises(KeyError, match="nope_type"):
        nc.channel_type("nope_type")


@pytest.mark.parametrize(
    "key", ["password", "token", "webhook_url", "secret", "api_key", "bot_token"]
)
def test_should_reject_inline_secret_key(key):
    with pytest.raises(nc.ChannelConfigError) as exc:
        FakeChannel.from_settings("inst", {key: "hunter2"}, {})
    assert key in str(exc.value) and "inst" in str(exc.value)
    assert "hunter2" not in str(exc.value)


def test_should_allow_env_indirection_key():
    ch = FakeChannel.from_settings("inst", {"token_env": "MY_TOKEN"}, {})
    assert ch.name == "inst"


def test_should_resolve_env_name_through_mapping():
    assert (
        nc.resolve_env(
            "inst", {"token_env": "MY_TOKEN"}, "token_env", {"MY_TOKEN": "v"}
        )
        == "v"
    )


def test_should_raise_when_env_var_missing_without_leaking():
    with pytest.raises(nc.ChannelConfigError) as exc:
        nc.resolve_env("inst", {"token_env": "MY_TOKEN"}, "token_env", {})
    assert "token_env" in str(exc.value)


def test_should_raise_when_env_setting_absent():
    with pytest.raises(nc.ChannelConfigError, match="token_env"):
        nc.resolve_env("inst", {}, "token_env", {})


def test_channel_config_error_is_value_error():
    assert issubclass(nc.ChannelConfigError, ValueError)


def test_should_build_outbox_path_under_plan_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    p = nc.channel_outbox_path("myplan", FakeChannel("slack-a"))
    assert p == tmp_path / "myplan.outbox.slack-a.jsonl"
    assert isinstance(p, Path)


@pytest.mark.parametrize("plan", ["a/b", "a\\b", "..", "x..y/z", ""])
def test_should_reject_plan_traversal(monkeypatch, tmp_path, plan):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    with pytest.raises(ValueError):
        nc.channel_outbox_path(plan, FakeChannel("a"))


@pytest.mark.parametrize("name", ["", "Up", "1a", "a" * 33, "a b", "a/b"])
def test_should_reject_invalid_instance_name(name):
    with pytest.raises(nc.ChannelConfigError):
        nc.validate_instance_name(name)


def test_should_accept_valid_instance_name():
    nc.validate_instance_name("team-a_1")
