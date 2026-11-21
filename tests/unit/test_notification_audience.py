import json
import logging
import sys
import types
import uuid

import pytest

from pipeline import notification_channels as nc
from pipeline.notification_audience import AUDIENCE_PLUGIN_ENV, members_for
from pipeline.notification_routing import (
    NotificationConfigError,
    load_notification_config,
)


class _AudChannel:
    type_name = "audtest"

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, name, settings, env):
        assert "principal" not in settings
        return cls(name)

    def send(self, record):
        return nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    if "audtest" not in nc.registered_types():
        nc.register_channel_type(_AudChannel)
    monkeypatch.delenv(AUDIENCE_PLUGIN_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_PLUGIN_MODULES", raising=False)


def _plugin(monkeypatch, members):
    name = "aud_plugin_" + uuid.uuid4().hex
    mod = types.ModuleType(name)

    class _P:
        def members(self, namespace, plan, audience, owner):
            return members(namespace, plan, audience, owner)

    mod.build = lambda: _P()
    mod.build.__module__ = name
    sys.modules[name] = mod
    monkeypatch.setenv(AUDIENCE_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv("PIPELINE_PLUGIN_MODULES", name)


def _cfg(tmp_path, deliver, instances=None):
    data = {
        "version": 1,
        "instances": instances
        or {
            "alice": {"type": "audtest", "principal": "alice"},
            "bob": {"type": "audtest", "principal": "bob"},
            "shared": {"type": "audtest"},
        },
        "routes": [{"name": "r", "match": {"plan": "*"}, "deliver": deliver}],
    }
    p = tmp_path / "n.json"
    p.write_text(json.dumps(data))
    return load_notification_config(p, env={})


def _resolve(cfg, owner="alice"):
    return cfg.resolve("plan", "parked", owner, members_for("default", "plan", owner))


def test_plan_owner_audience_delivers_to_owner_only(tmp_path):
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_owner"]}])
    assert _resolve(cfg, "alice") == ["alice"]


def test_plan_owner_audience_with_no_owner_delivers_nowhere(tmp_path):
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_owner"]}])
    assert _resolve(cfg, "") == []


def test_plan_approvers_via_plugin_deliver_to_each_approver(tmp_path, monkeypatch):
    _plugin(monkeypatch, lambda ns, plan, aud, owner: frozenset({"alice", "bob"}))
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_approvers"]}])
    assert sorted(_resolve(cfg)) == ["alice", "bob"]


def test_plan_approvers_without_plugin_deliver_to_nobody(tmp_path):
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_approvers"]}])
    assert _resolve(cfg) == []


def test_to_and_audience_deliver_the_intersection(tmp_path, monkeypatch):
    _plugin(monkeypatch, lambda ns, plan, aud, owner: frozenset({"alice"}))
    cfg = _cfg(
        tmp_path,
        [{"events": ["*"], "to": ["alice", "bob"], "audience": ["plan_approvers"]}],
    )
    assert _resolve(cfg) == ["alice"]


def test_to_and_audience_never_deliver_an_instance_without_principal(tmp_path):
    cfg = _cfg(
        tmp_path, [{"events": ["*"], "to": ["shared"], "audience": ["plan_owner"]}]
    )
    assert _resolve(cfg) == []


def test_to_only_entry_is_unchanged(tmp_path):
    cfg = _cfg(tmp_path, [{"events": ["*"], "to": ["shared", "bob"]}])
    assert cfg.resolve("plan", "parked", "alice") == ["shared", "bob"]


def test_audience_members_none_means_every_audience_is_empty(tmp_path):
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_owner"]}])
    assert cfg.resolve("plan", "parked", "alice") == []


def test_raising_plugin_delivers_to_nobody_and_logs_no_subject(
    tmp_path, monkeypatch, caplog
):
    def boom(*_a):
        raise RuntimeError("secret-subject-alice")

    _plugin(monkeypatch, boom)
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_approvers"]}])
    with caplog.at_level(logging.WARNING):
        assert _resolve(cfg) == []
    assert "RuntimeError" in caplog.text
    assert "secret-subject-alice" not in caplog.text


def test_plugin_returning_list_of_ints_delivers_to_nobody(tmp_path, monkeypatch, caplog):
    _plugin(monkeypatch, lambda *_a: [1, 2])
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_approvers"]}])
    with caplog.at_level(logging.WARNING):
        assert _resolve(cfg) == []
    assert "alice" not in caplog.text


def test_plugin_returning_list_of_strings_delivers_to_nobody(tmp_path, monkeypatch):
    _plugin(monkeypatch, lambda *_a: ["alice"])
    cfg = _cfg(tmp_path, [{"events": ["*"], "audience": ["plan_approvers"]}])
    assert _resolve(cfg) == []


@pytest.mark.parametrize("bad", ["", "x" * 256, "a\nb"])
def test_invalid_principal_is_refused(tmp_path, bad):
    with pytest.raises(NotificationConfigError, match=r"instances\.alice\.principal"):
        _cfg(
            tmp_path,
            [{"events": ["*"], "to": ["alice"]}],
            instances={"alice": {"type": "audtest", "principal": bad}},
        )


def test_principal_of_255_characters_is_accepted(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"events": ["*"], "to": ["alice"]}],
        instances={"alice": {"type": "audtest", "principal": "x" * 255}},
    )
    assert cfg.principals == {"alice": "x" * 255}


def test_unknown_audience_is_refused_with_key_path(tmp_path):
    with pytest.raises(NotificationConfigError, match=r"routes\[0\]\.deliver\[0\]\.audience\[0\]"):
        _cfg(tmp_path, [{"events": ["*"], "audience": ["everyone"]}])


def test_deliver_entry_with_neither_to_nor_audience_is_refused(tmp_path):
    with pytest.raises(NotificationConfigError, match=r"routes\[0\]\.deliver\[0\]"):
        _cfg(tmp_path, [{"events": ["*"]}])


def test_empty_audience_list_is_refused(tmp_path):
    with pytest.raises(NotificationConfigError, match=r"audience"):
        _cfg(tmp_path, [{"events": ["*"], "audience": []}])
