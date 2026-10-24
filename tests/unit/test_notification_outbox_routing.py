"""Routing, spooling and draining of per-instance notification outboxes."""

import json
import logging
from typing import ClassVar

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline import paths, persistence
from pipeline.notification_channels import DeliveryResult, DeliveryStatus


class FakeChannel:
    type_name = "fake_nc04"
    received: ClassVar[dict] = {}
    behaviour: ClassVar[dict] = {}

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        return cls(instance_name)

    def send(self, record):
        action = self.behaviour.get(self.name, DeliveryStatus.DELIVERED)
        if isinstance(action, Exception):
            raise action
        if action is DeliveryStatus.DELIVERED:
            self.received.setdefault(self.name, []).append(
                (record["plan"], record["payload"]["event"])
            )
        return DeliveryResult(action)


TWO_TEAM = {
    "version": 1,
    "instances": {
        "email-a": {"type": "fake_nc04"},
        "slack-a": {"type": "fake_nc04"},
        "email-b": {"type": "fake_nc04"},
    },
    "routes": [
        {
            "name": "team-a",
            "match": {"plan": "team-a-*"},
            "deliver": [
                {"events": ["plan_completed"], "to": ["email-a"]},
                {"events": ["story_parked", "dispatch_failed"], "to": ["slack-a"]},
            ],
        },
        {
            "name": "team-b",
            "match": {"plan": "team-b-*"},
            "deliver": [{"events": ["*"], "to": ["email-b"]}],
        },
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ob, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(nc, "_REGISTRY", {"fake_nc04": FakeChannel})
    monkeypatch.setattr(FakeChannel, "received", {})
    monkeypatch.setattr(FakeChannel, "behaviour", {})
    monkeypatch.delenv(ob.OUTBOX_ENABLED_ENV, raising=False)
    monkeypatch.delenv(ob.OUTBOX_EVENTS_ENV, raising=False)
    cfg = tmp_path / "cfg" / "notifications.json"
    cfg.parent.mkdir()
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(cfg))
    return tmp_path


def _write_config(env, data=None):
    (env / "cfg" / "notifications.json").write_text(json.dumps(data or TWO_TEAM))


def _emit(plan, name):
    ob.outbox_sink({"type": "notification", "plan": plan, "payload": {"event": name}})


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def test_should_route_two_teams_end_to_end(env):
    _write_config(env)
    for plan in ("team-a-build", "team-b-build"):
        for name in ("plan_completed", "story_parked", "dispatch_failed"):
            _emit(plan, name)

    result = ob.drain_channel_outboxes()

    assert FakeChannel.received == {
        "email-a": [("team-a-build", "plan_completed")],
        "slack-a": [("team-a-build", "story_parked"), ("team-a-build", "dispatch_failed")],
        "email-b": [
            ("team-b-build", "plan_completed"),
            ("team-b-build", "story_parked"),
            ("team-b-build", "dispatch_failed"),
        ],
    }
    assert result == {"email-a": 1, "slack-a": 2, "email-b": 3}


def test_should_not_spool_event_no_route_delivers(env):
    _write_config(env)
    _emit("team-a-build", "tests_failed")
    assert not list(env.glob("*.outbox.*.jsonl"))


def test_should_ignore_legacy_events_allowlist_for_instances(env, monkeypatch):
    _write_config(env)
    monkeypatch.setenv(ob.OUTBOX_ENABLED_ENV, "1")
    monkeypatch.setenv(ob.OUTBOX_EVENTS_ENV, "something_else")
    _emit("team-b-build", "custom_event")
    assert len(_lines(env / "team-b-build.outbox.email-b.jsonl")) == 1
    assert not (env / "team-b-build.outbox.jsonl").exists()


def test_should_write_identical_line_to_legacy_and_instance(env, monkeypatch):
    _write_config(env)
    monkeypatch.setenv(ob.OUTBOX_ENABLED_ENV, "1")
    _emit("team-a-build", "plan_completed")
    legacy = _lines(env / "team-a-build.outbox.jsonl")
    assert legacy and legacy == _lines(env / "team-a-build.outbox.email-a.jsonl")


def test_should_leave_legacy_output_unchanged_without_config_file(env, monkeypatch):
    monkeypatch.setenv(ob.OUTBOX_ENABLED_ENV, "1")
    _emit("team-a-build", "plan_completed")
    assert [p.name for p in env.glob("*.jsonl")] == ["team-a-build.outbox.jsonl"]


def test_should_spool_to_no_instance_when_config_is_broken(env, monkeypatch):
    (env / "cfg" / "notifications.json").write_text("{not json")
    monkeypatch.setenv(ob.OUTBOX_ENABLED_ENV, "1")
    _emit("team-a-build", "plan_completed")
    assert [p.name for p in env.glob("*.jsonl")] == ["team-a-build.outbox.jsonl"]


def test_should_retry_only_the_instance_that_returned_retry(env):
    _write_config(env)
    FakeChannel.behaviour = {"slack-a": DeliveryStatus.RETRY}
    _emit("team-a-build", "plan_completed")
    _emit("team-a-build", "story_parked")

    first = ob.drain_channel_outboxes()
    second = ob.drain_channel_outboxes()

    assert first["email-a"] == 1 and second["email-a"] == 0
    assert first["slack-a"] == 0
    assert len(_lines(env / "team-a-build.outbox.slack-a.jsonl")) == 1
    assert _lines(env / "team-a-build.outbox.email-a.jsonl") == []


def test_should_drop_rejected_records(env):
    _write_config(env)
    FakeChannel.behaviour = {"slack-a": DeliveryStatus.REJECTED}
    _emit("team-a-build", "story_parked")
    ob.drain_channel_outboxes()
    assert _lines(env / "team-a-build.outbox.slack-a.jsonl") == []


def test_should_not_block_other_instance_when_one_raises(env):
    _write_config(env)
    FakeChannel.behaviour = {"slack-a": RuntimeError("boom")}
    _emit("team-a-build", "plan_completed")
    _emit("team-a-build", "story_parked")

    result = ob.drain_channel_outboxes()

    assert result["email-a"] == 1
    assert len(_lines(env / "team-a-build.outbox.slack-a.jsonl")) == 1


def test_should_continue_to_other_instances_when_one_write_fails(env, monkeypatch, caplog):
    cfg = json.loads(json.dumps(TWO_TEAM))
    cfg["routes"][0]["deliver"] = [{"events": ["plan_completed"], "to": ["slack-a", "email-a"]}]
    _write_config(env, cfg)
    monkeypatch.setenv(ob.OUTBOX_ENABLED_ENV, "1")
    real = ob._append_rotating

    def flaky(path, line):
        if "slack-a" in path.name:
            raise OSError("secret-path-detail")
        real(path, line)

    monkeypatch.setattr(ob, "_append_rotating", flaky)
    with caplog.at_level(logging.ERROR):
        _emit("team-a-build", "plan_completed")

    assert len(_lines(env / "team-a-build.outbox.email-a.jsonl")) == 1
    assert len(_lines(env / "team-a-build.outbox.jsonl")) == 1
    assert "secret-path-detail" not in caplog.text
    assert "OSError" in caplog.text


def test_should_drain_only_named_plan(env):
    _write_config(env)
    _emit("team-a-build", "plan_completed")
    _emit("team-a-other", "plan_completed")

    result = ob.drain_channel_outboxes("team-a-build")

    assert result["email-a"] == 1
    assert len(_lines(env / "team-a-other.outbox.email-a.jsonl")) == 1


def test_should_leave_files_of_removed_instances_untouched(env):
    _write_config(env)
    orphan = env / "team-a-build.outbox.gone.jsonl"
    orphan.write_text('{"plan": "team-a-build", "payload": {"event": "plan_completed"}}\n')
    before = orphan.read_bytes()

    result = ob.drain_channel_outboxes()

    assert "gone" not in result
    assert orphan.read_bytes() == before


def test_should_return_empty_without_config_file(env):
    assert ob.drain_channel_outboxes() == {}


def test_should_keep_records_appended_during_drain(env, monkeypatch):
    _write_config(env)
    _emit("team-a-build", "plan_completed")
    original = FakeChannel.send

    def send_and_spool(self, record):
        _emit("team-a-build", "plan_completed")
        return original(self, record)

    monkeypatch.setattr(FakeChannel, "send", send_and_spool)
    result = ob.drain_channel_outboxes()

    assert result["email-a"] == 1
    assert len(_lines(env / "team-a-build.outbox.email-a.jsonl")) == 1
