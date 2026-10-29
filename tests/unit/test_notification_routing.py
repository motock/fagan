import json
import logging

import pytest

from pipeline import notification_channels as nc
from pipeline.notification_routing import (
    NotificationConfigError,
    load_notification_config,
    safe_load_notification_config,
)


class _FakeChannel:
    type_name = "fakeroute"

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, name, settings, env):
        return cls(name)

    def send(self, record):
        return nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)


@pytest.fixture(autouse=True)
def _register_fake():
    if "fakeroute" not in nc.registered_types():
        nc.register_channel_type(_FakeChannel)
    yield


def _write(tmp_path, data):
    p = tmp_path / "notifications.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def _cfg(**over):
    base = {
        "version": 1,
        "instances": {"mail": {"type": "fakeroute"}, "slack": {"type": "fakeroute"}},
        "routes": [
            {"name": "a", "match": {"plan": "team-a-*"},
             "deliver": [{"events": ["*"], "to": ["mail"]}]},
            {"name": "b", "match": {"plan": "team-b-*"},
             "deliver": [{"events": ["parked", "failed"], "to": ["slack"]}]},
        ],
    }
    base.update(over)
    return base


def _load(tmp_path, data):
    return load_notification_config(_write(tmp_path, data), env={})


def test_absent_file_returns_none(tmp_path):
    assert load_notification_config(tmp_path / "nope.json", env={}) is None


def test_env_path_override_is_used(tmp_path):
    p = _write(tmp_path, _cfg())
    cfg = load_notification_config(env={"PIPELINE_NOTIFY_CONFIG_PATH": str(p)})
    assert set(cfg.instances) == {"mail", "slack"}


def test_team_a_gets_everything_by_email_only(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-a-x", "merged") == ["mail"]


def test_team_b_parked_goes_to_slack_only(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-b-x", "parked") == ["slack"]


def test_unlisted_event_goes_nowhere(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-b-x", "merged") == []


def test_none_event_matches_only_wildcard(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-b-x", None) == []
    assert cfg.resolve("team-a-x", None) == ["mail"]


def test_no_matching_route_goes_nowhere(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("other", "parked") == []


def test_first_matching_route_wins(tmp_path):
    routes = [
        {"name": "one", "match": {"plan": "x-*"}, "deliver": [{"events": ["a"], "to": ["mail"]}]},
        {"name": "two", "match": {"plan": "x-*"}, "deliver": [{"events": ["*"], "to": ["slack"]}]},
    ]
    cfg = _load(tmp_path, _cfg(routes=routes))
    assert cfg.resolve("x-1", "b") == []


def test_union_of_matching_deliver_entries(tmp_path):
    routes = [{"name": "u", "match": {"plan": "*"}, "deliver": [
        {"events": ["a"], "to": ["mail"]}, {"events": ["*"], "to": ["slack", "mail"]}]}]
    cfg = _load(tmp_path, _cfg(routes=routes))
    assert sorted(cfg.resolve("p", "a")) == ["mail", "slack"]


def test_glob_is_case_sensitive(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("TEAM-A-x", "merged") == []


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: c.update(version=2), "version"),
    (lambda c: c.update(version=True), "version"),
    (lambda c: c["instances"].update(bad={"type": "nosuchtype"}), "unknown channel type"),
    (lambda c: c["instances"].update({"Bad Name": {"type": "fakeroute"}}), "invalid instance name"),
    (lambda c: c["instances"].update(x="str"), "must be an object"),
    (lambda c: c["instances"].update(x={"type": "fakeroute", "password": "hunter2"}), "inline"),
    (lambda c: c["routes"][0]["deliver"][0].update(to=["ghost"]), "unknown instance"),
    (lambda c: c["routes"][0]["deliver"][0].update(to=[]), "must not be empty"),
    (lambda c: c["routes"][0]["deliver"][0].update(events=[]), "must not be empty"),
    (lambda c: c["routes"][0]["deliver"][0].update(events=[1]), "non-empty string"),
    (lambda c: c["routes"].__setitem__(0, "str"), "must be an object"),
    (lambda c: c["routes"][0]["match"].update(other="x"), "not supported yet"),
    (lambda c: c["routes"][0].pop("match"), "match"),
])
def test_validation_failures_raise(tmp_path, mutate, fragment):
    c = _cfg()
    mutate(c)
    with pytest.raises(NotificationConfigError, match=fragment):
        _load(tmp_path, c)


def test_inline_secret_error_does_not_echo_value(tmp_path):
    c = _cfg()
    c["instances"]["x"] = {"type": "fakeroute", "password": "hunter2"}
    with pytest.raises(NotificationConfigError) as ei:
        _load(tmp_path, c)
    assert "hunter2" not in str(ei.value)


def test_bad_json_raises(tmp_path):
    with pytest.raises(NotificationConfigError, match="not valid JSON"):
        _load(tmp_path, "{nope")


def test_duplicate_instance_name_raises(tmp_path):
    raw = (
        '{"version": 1, "instances": {"mail": {"type": "fakeroute"}, '
        '"mail": {"type": "fakeroute"}}, "routes": []}'
    )
    with pytest.raises(NotificationConfigError, match="duplicate key 'mail'"):
        _load(tmp_path, raw)


def test_top_level_non_object_raises(tmp_path):
    with pytest.raises(NotificationConfigError, match="must be an object"):
        _load(tmp_path, "[]")


def test_oversized_file_raises(tmp_path):
    with pytest.raises(NotificationConfigError, match="exceeds"):
        _load(tmp_path, " " * (256 * 1024 + 1))


def test_safe_load_broken_file_returns_empty_config_and_logs_error(tmp_path, caplog):
    p = _write(tmp_path, "{nope")
    with caplog.at_level(logging.ERROR):
        cfg = safe_load_notification_config(p, env={})
    assert cfg is not None
    assert cfg.instances == {}
    assert cfg.resolve("team-a-x", "merged") == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_safe_load_absent_file_is_legacy_none(tmp_path):
    assert safe_load_notification_config(tmp_path / "nope.json", env={}) is None
