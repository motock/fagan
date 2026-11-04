"""Tests for operator-selected notification channel plugins.

A channel plugin is chosen only by ``PIPELINE_CHANNEL_PLUGINS`` (an ordered
comma-separated list of ``module:attr`` specs). Each entry resolves to a
registrar callable that is handed ``register_channel_type`` and calls it for
every channel class it contributes. Registration runs once per process,
because ``register_channel_type`` raises on a duplicate type name while the
config loader may run many times.

The registry is global, so the fixture snapshots and restores it; otherwise a
type registered by one test would leak into the next and a duplicate-name
error would be raised where none is expected.
"""

from __future__ import annotations

import json

import pytest

from app.plugin_loader import PluginError
from pipeline import notification_channels as nc
from pipeline import notification_email, notification_slack
from pipeline import notification_routing as routing

# Importing the built-in channel modules is what registers "email" and "slack".
# Holding a reference keeps the import used as far as a linter is concerned.
_BUILTIN_CHANNEL_MODULES = (notification_email, notification_slack)

ENV = "PIPELINE_CHANNEL_PLUGINS"

# Registrar calls in order, so "applied once" and "applied in order" are both
# observable.
CALLS: list[str] = []


class FakeChatChannel:
    type_name = "fakechat"

    def __init__(self, name):
        self.name = name

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        nc.reject_inline_secrets(instance_name, settings)
        return cls(instance_name)


class SecondChatChannel:
    type_name = "secondchat"

    def __init__(self, name):
        self.name = name

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        nc.reject_inline_secrets(instance_name, settings)
        return cls(instance_name)


class DuplicateEmailChannel:
    # A name the built-in set already owns; registering it must raise.
    type_name = "email"


def register_fakechat(register):
    CALLS.append("fakechat")
    register(FakeChatChannel)


def register_secondchat(register):
    CALLS.append("secondchat")
    register(SecondChatChannel)


def register_duplicate_email(register):
    CALLS.append("duplicate-email")
    register(DuplicateEmailChannel)


def register_that_raises(register):
    CALLS.append("raises")
    raise RuntimeError("plugin blew up")


@pytest.fixture(autouse=True)
def isolated_channel_state():
    """Snapshot/restore the global channel registry and the once-per-process set."""
    snapshot = dict(nc._REGISTRY)
    routing._REGISTERED_CHANNEL_PLUGINS.clear()
    CALLS.clear()
    try:
        yield
    finally:
        nc._REGISTRY.clear()
        nc._REGISTRY.update(snapshot)
        routing._REGISTERED_CHANNEL_PLUGINS.clear()
        CALLS.clear()


def _write_config(tmp_path, instances):
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps({"version": 1, "instances": instances, "routes": []}),
        encoding="utf-8",
    )
    return path


def _spec(attr):
    return f"{__name__}:{attr}"


def test_should_leave_channel_types_unchanged_when_no_plugin_configured(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(ENV, raising=False)
    before = nc.registered_types()

    config = routing.load_notification_config(_write_config(tmp_path, {}))

    assert config is not None
    assert nc.registered_types() == before
    assert {"email", "slack"} <= set(nc.registered_types())
    assert not routing._REGISTERED_CHANNEL_PLUGINS
    assert CALLS == []


def test_should_register_plugin_channel_type_and_load_its_config(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _spec("register_fakechat"))

    config = routing.load_notification_config(
        _write_config(tmp_path, {"chat": {"type": "fakechat"}})
    )

    assert "fakechat" in nc.registered_types()
    assert config is not None
    assert config.instances["chat"].name == "chat"


def test_should_call_registrar_once_across_repeated_loads(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _spec("register_fakechat"))
    path = _write_config(tmp_path, {"chat": {"type": "fakechat"}})

    routing.load_notification_config(path)
    routing.load_notification_config(path)

    assert CALLS == ["fakechat"]


def test_should_apply_two_plugins_in_listed_order(tmp_path, monkeypatch):
    monkeypatch.setenv(
        ENV, f"{_spec('register_fakechat')},{_spec('register_secondchat')}"
    )

    routing.load_notification_config(
        _write_config(
            tmp_path, {"one": {"type": "fakechat"}, "two": {"type": "secondchat"}}
        )
    )

    assert CALLS == ["fakechat", "secondchat"]
    assert {"fakechat", "secondchat"} <= set(nc.registered_types())


def test_should_propagate_registrar_error_and_retry_on_next_load(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _spec("register_that_raises"))
    path = _write_config(tmp_path, {})

    with pytest.raises(RuntimeError):
        routing.load_notification_config(path)
    assert not routing._REGISTERED_CHANNEL_PLUGINS

    with pytest.raises(RuntimeError):
        routing.load_notification_config(path)
    assert CALLS == ["raises", "raises"]


def test_should_raise_plugin_error_for_invalid_spec(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "not a valid spec")

    with pytest.raises(PluginError):
        routing.load_notification_config(_write_config(tmp_path, {}))


def test_should_raise_when_registrar_registers_existing_type(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _spec("register_duplicate_email"))

    with pytest.raises(ValueError, match="channel type already registered"):
        routing.load_notification_config(_write_config(tmp_path, {}))
    assert not routing._REGISTERED_CHANNEL_PLUGINS
